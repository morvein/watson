"""Current Amazon identifier form, with optional user-completed CAPTCHA."""
import asyncio
import sys
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import LookupResult, failure_result
from .common import HttpFailure, Result, Status, parse_retry_after


LOGIN_URL = (
    "https://www.amazon.com/ap/signin"
    "?openid.return_to=https%3A%2F%2Fwww.amazon.com%2F%3Fref_%3Dnav_ya_signin"
    "&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
    "&openid.assoc_handle=usflex&openid.mode=checkid_setup"
    "&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
    "&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0"
)
IDENTIFIER_PATHS = {"/ax/claim", "/ap/signin", "/ap/signin/"}
RESULT_PATHS = IDENTIFIER_PATHS | {"/ax/claim/intent", "/ap/register", "/ap/register/"}
CAPTCHA_PATHS = {"/errors_page/validateCaptcha", "/errors/validateCaptcha",
                 "/checkpoint/continue", "/checkpoint/validate", "/checkpoint/get"}
CAPTCHA_REASON = "Amazon requested CAPTCHA. Use --amazon-interactive and complete it in Chrome; registration status is unknown."


def trusted_url(value, base=LOGIN_URL):
    url = urlsplit(urljoin(base, value))
    if (url.scheme != "https" or url.hostname != "www.amazon.com"
            or url.username or url.password or url.port not in (None, 443)):
        raise ValueError("Unexpected Amazon destination")
    return url


def parse_page(text):
    root = BeautifulSoup(text, "html.parser")
    if root.select('form[action*="validateCaptcha"], form[action*="/checkpoint/validate"], #captchacharacters, #continueCheck'):
        return LookupResult("action_required", CAPTCHA_REASON)
    # Autofill hints are present on the initial identifier page. Their password
    # inputs must never be treated as an existing account.
    password = root.select_one('input#ap_password[name="password"]')
    found = bool(password and password.get("type") == "password")
    if found:
        for element in (password, *password.parents):
            style = element.get("style", "").replace(" ", "").lower()
            if (element.has_attr("hidden") or element.get("aria-hidden") == "true"
                    or "display:none" in style or "visibility:hidden" in style):
                found = False
                break
    visible_text = root.get_text(" ", strip=True).lower()
    absent = any(marker in visible_text for marker in (
        "looks like you're new to amazon", "we cannot find an account with that",
        "we can't find an account with that", "we could not find an account with that",
    ))
    if found and absent:
        raise ValueError("Conflicting Amazon result")
    if found:
        return LookupResult("found", "Amazon returned a password form for the identifier.")
    if absent:
        return LookupResult("not_found", "Amazon explicitly reported that no account was found for the identifier.")
    return None


class AmazonGuard:
    def __init__(self, phone, timeout, interactive=False):
        self.phone, self.timeout, self.interactive = phone, timeout, interactive
        self.identifier_path = None
        self.submitted = False
        self.captcha_posts = 0
        self.notified = False
        self.reply = asyncio.get_running_loop().create_future()

    def finish(self, lookup):
        if not self.reply.done():
            self.reply.set_result(lookup)

    def accept_page(self, text):
        lookup = parse_page(text)
        if lookup and lookup.outcome == "action_required" and self.interactive and not self.notified:
            self.notified = True
            print("Amazon: complete CAPTCHA in the Chrome window. No password, SMS or code is requested.",
                  file=sys.stderr, flush=True)
        if lookup and (lookup.outcome != "action_required" or not self.interactive):
            self.finish(lookup)

    async def __call__(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        if request.method in ("GET", "HEAD", "OPTIONS"):
            if (self.submitted and request.is_navigation_request()
                    and request.frame.parent_frame is None):
                try:
                    target = trusted_url(request.url)
                    allowed = target.path in RESULT_PATHS | CAPTCHA_PATHS
                except ValueError:
                    allowed = False
                if not allowed:
                    self.finish(LookupResult("action_required", "Amazon requested a different verification step. No code or sign-in was submitted; registration status is unknown."))
                    await route.abort()
                    return
            await route.continue_()
            return
        if request.method != "POST" or parsed.hostname != "www.amazon.com" or parsed.scheme != "https":
            await route.abort()
            return
        try:
            trusted_url(request.url)
        except ValueError:
            await route.abort()
            return
        data = parse_qs(request.post_data or "", keep_blank_values=True)
        sensitive = any(values != [""] for key, values in data.items()
                        if key.lower() in ("password", "otpcode", "otp", "code", "verificationcode"))
        if (self.interactive and parsed.path in CAPTCHA_PATHS and self.captcha_posts < 3
                and not sensitive and not any(key in data for key in ("email", "phone", "phoneNumber"))):
            self.captcha_posts += 1
            await route.continue_()
            return
        if (self.reply.done() or self.submitted or sensitive
                or parsed.path != self.identifier_path or parsed.path not in IDENTIFIER_PATHS
                or data.get("email") != [self.phone.e164]):
            await route.abort()
            return
        self.submitted = True
        delivered = False
        try:
            response = await route.fetch(max_redirects=0, max_retries=0, timeout=self.timeout)
            if response.status in (301, 302, 303):
                target = trusted_url(response.headers.get("location", ""), request.url)
                if target.path not in RESULT_PATHS | CAPTCHA_PATHS:
                    self.finish(LookupResult("action_required", "Amazon requested additional verification; no code requested, registration status is unknown."))
                    return
            elif response.status == 200:
                self.accept_page(await response.text())
            else:
                raise HttpFailure(f"http_{response.status}", response.status,
                                  parse_retry_after(response.headers.get("retry-after")))
            if not self.reply.done():
                await route.fulfill(response=response)
                delivered = True
        except Exception as error:
            if not self.reply.done():
                self.reply.set_exception(error)
        finally:
            if not delivered:
                await route.abort()

    async def observe(self, response):
        request = response.request
        if (not self.submitted or self.reply.done() or request.method != "GET"
                or not request.is_navigation_request() or request.frame.parent_frame is not None):
            return
        try:
            target = trusted_url(response.url)
            if target.path not in RESULT_PATHS | CAPTCHA_PATHS:
                return
            if response.status == 200:
                self.accept_page(await response.text())
            elif response.status >= 400:
                raise HttpFailure(f"http_{response.status}", response.status,
                                  parse_retry_after(response.headers.get("retry-after")))
        except Exception as error:
            if not self.reply.done():
                self.reply.set_exception(error)


async def browser_lookup(phone, timeout, interactive=False):
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=not interactive)
        guard = None
        try:
            context = await browser.new_context(locale="en-US", service_workers="block")
            page = await context.new_page()
            page.set_default_timeout(timeout * 1000)
            response = await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            if response is not None and response.status != 200:
                raise HttpFailure(f"http_{response.status}", response.status)
            guard = AmazonGuard(phone, timeout * 1000, interactive)
            await context.route("**/*", guard)
            page.on("response", guard.observe)
            initial = parse_page(await page.content())
            field = page.locator("#ap_email_login, #ap_email").first
            if initial and initial.outcome == "action_required":
                if not interactive:
                    return initial
                print("Amazon: complete CAPTCHA in Chrome and click Continue shopping. Waiting up to 5 minutes.",
                      file=sys.stderr, flush=True)
                await page.wait_for_function("!document.querySelector('form[action*=validateCaptcha], form[action*=\"/checkpoint/validate\"], #captchacharacters, #continueCheck')", timeout=300000)
                if not await field.is_visible():
                    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            await field.wait_for(state="visible", timeout=300000 if interactive else timeout * 1000)
            action = await field.evaluate("el => el.form.action")
            target = trusted_url(action)
            if target.path not in IDENTIFIER_PATHS:
                raise ValueError("Unknown Amazon form")
            guard.identifier_path = target.path
            await field.fill(phone.e164)
            button = field.locator("xpath=ancestor::form").locator('input[type="submit"], button[type="submit"]').first
            await button.click()
            try:
                return await asyncio.wait_for(guard.reply, max(timeout, 300) if interactive else timeout)
            except asyncio.TimeoutError:
                last = parse_page(await page.content())
                if last is not None:
                    return last
                return LookupResult("unknown", "Amazon did not return a conclusive lookup result; registration status is unknown.")
        finally:
            if guard is not None:
                if guard.reply.done() and not guard.reply.cancelled():
                    guard.reply.exception()
                elif not guard.reply.done():
                    guard.reply.cancel()
            await browser.close()


class AmazonWebAdapter:
    name = "amazon"
    method = "browser_identifier_lookup"
    source_url = LOGIN_URL
    requirement = "--allow-recovery; Chrome; /ax/claim identifier form. Manual CAPTCHA: --amazon-interactive; no SMS/OTP."

    async def check(self, phone, http, tokens):
        if not tokens.get("_allow_recovery"):
            return Result(self.name, Status.USER_ACTION_REQUIRED, self.method, self.requirement, source_url=LOGIN_URL)
        try:
            async with http.locks.setdefault(self.name, asyncio.Lock()):
                lookup = await browser_lookup(phone, http.timeout, bool(tokens.get("_amazon_interactive")))
            status = {"found": Status.LINKED, "not_found": Status.NOT_LINKED,
                      "action_required": Status.USER_ACTION_REQUIRED, "unknown": Status.UNKNOWN}[lookup.outcome]
            return Result(self.name, status, self.method, lookup.evidence, source_url=LOGIN_URL)
        except HttpFailure as error:
            return failure_result(self, error)
        except (ValueError, KeyError, TypeError):
            return Result(self.name, Status.UNKNOWN, self.method, "Unrecognized Amazon response; registration status is unknown.",
                          source_url=LOGIN_URL, error_code="unrecognized_amazon_response")
        except Exception as error:
            timeout = "timeout" in type(error).__name__.lower()
            return Result(self.name, Status.USER_ACTION_REQUIRED, self.method,
                          "Amazon lookup did not finish. For manual CAPTCHA use --amazon-interactive; check Chrome and dependencies.",
                          source_url=LOGIN_URL, error_code="amazon_timeout" if timeout else "amazon_browser_unavailable")


adapter = AmazonWebAdapter()
