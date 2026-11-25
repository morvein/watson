"""Identifier lookup with bounded, site-driven CAPTCHA continuation only."""
import asyncio
import sys
from urllib.parse import urlsplit

from .base import failure_result
from .common import HttpFailure, Result, Status


LOGIN = "https://accounts.snapchat.com/v2/login"
WEB_LOGIN = "https://accounts.snapchat.com/snapchat.janus.api.WebLoginService/WebLogin"
BOOTSTRAP = "https://session.snapchat.com/snap.security.WebAttestationService/BootstrapAttestationSession"
INITIAL_REQUESTS = {
    BOOTSTRAP: 1,
    "https://accounts.snapchat.com/accounts/sso": 2,
    "https://accounts.snapchat.com/snapchat.auth.passkey.api.external.PasskeyExternalService/GetPasskeyLoginOptions": 1,
    "https://aws.api.snapchat.com/snapchat.cdp.cof.CircumstancesService/targetingQuery": 1,
}


def national_digits(phone):
    return phone.e164[1 + len(str(phone.country_code)):]


def protobuf_fields(data):
    """Read bounded protobuf scalars without retaining authentication payloads."""
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Oversized protobuf")
    pos, fields = 0, {}

    def varint():
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(data):
                raise ValueError("Truncated protobuf")
            byte = data[pos]
            pos += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
        raise ValueError("Invalid varint")

    while pos < len(data):
        tag = varint()
        field, wire = tag >> 3, tag & 7
        if not field or field in fields:
            raise ValueError("Ambiguous protobuf")
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            size = varint() if wire == 2 else (8 if wire == 1 else 4)
            if size > len(data) - pos:
                raise ValueError("Truncated field")
            value = data[pos:pos + size]
            pos += size
        else:
            raise ValueError("Unsupported wire type")
        fields[field] = value
    return fields


def grpc_message(data):
    pos, message = 0, None
    while pos < len(data):
        if len(data) - pos < 5:
            raise ValueError("Truncated gRPC frame")
        flag, size = data[pos], int.from_bytes(data[pos + 1:pos + 5], "big")
        pos += 5
        if size > len(data) - pos or size > 2 * 1024 * 1024:
            raise ValueError("Invalid gRPC length")
        payload = data[pos:pos + size]
        pos += size
        if flag == 128:
            trailers = dict(line.split(b":", 1) for line in payload.split(b"\r\n") if b":" in line)
            if trailers.get(b"grpc-status", b"0").strip() != b"0":
                raise ValueError("gRPC error")
        elif flag == 0 and message is None:
            message = payload
        else:
            raise ValueError("Unsupported or repeated gRPC frame")
    if message is None:
        raise ValueError("Missing gRPC message")
    return protobuf_fields(message)


def classify(data):
    fields = grpc_message(data)
    code = fields.get(1)
    if code == 11:  # WebLoginResponse.ERR_ACCOUNT_NOT_FOUND
        if any(key in fields for key in (6, 7, 13)):
            raise ValueError("Conflicting WebLogin result")
        return Status.NOT_LINKED, "Snapchat explicitly returned ERR_ACCOUNT_NOT_FOUND."
    if code == 2 and isinstance(fields.get(7), bytes):
        challenge = protobuf_fields(fields[7])
        if set(challenge) == {2} and isinstance(challenge[2], bytes):
            protobuf_fields(challenge[2])  # ChallengeData.passwordChallenge
            return Status.LINKED, "Snapchat returned a password challenge for the account."
        return Status.USER_ACTION_REQUIRED, "Snapchat requested additional verification; registration status is unknown."
    if code == 10:
        return Status.RATE_LIMITED, "Snapchat rate-limited the request; no automatic retry."
    return Status.UNKNOWN, "Snapchat did not confirm whether an account exists."


def is_captcha(fields):
    if fields.get(1) != 2 or not isinstance(fields.get(7), bytes):
        return False
    challenge = protobuf_fields(fields[7])
    return set(challenge) == {12} and isinstance(challenge[12], bytes)


class SubmissionGuard:
    def __init__(self, phone, timeout, on_captcha=None):
        self.phone, self.timeout = phone, timeout
        self.submitted = False
        self.captcha_pending = False
        self.captcha_count = 0
        self.on_captcha = on_captcha
        self.initial_requests = {}
        self.reply = asyncio.get_running_loop().create_future()

    async def __call__(self, route):
        request = route.request
        if request.method in ("GET", "HEAD", "OPTIONS"):
            await route.continue_()
            return
        if request.method == "POST" and request.url == WEB_LOGIN and not self.reply.done():
            deliver = False
            try:
                fields = grpc_message(request.post_data_buffer or b"")
                if not self.submitted:
                    self.submitted = True
                    if (fields.get(4) != self.phone.e164.encode() or 6 in fields
                            or set(fields) - {4, 8, 10}):
                        raise ValueError("Unexpected identifier or challenge answer")
                else:
                    # The site may answer only its immediately preceding CAPTCHA.
                    # No second identifier, password, OTP, channel selection or
                    # generic challenge-answer mutation is permitted.
                    answer = protobuf_fields(fields[6]) if isinstance(fields.get(6), bytes) else {}
                    if (not self.captcha_pending or self.captcha_count >= 2
                            or set(fields) - {6, 8, 10} or set(answer) != {13}
                            or not isinstance(answer[13], bytes)):
                        return
                    token = protobuf_fields(answer[13])
                    if set(token) != {1} or not isinstance(token[1], bytes) or not token[1]:
                        return
                    self.captcha_pending = False
                    self.captcha_count += 1
                response = await route.fetch(max_redirects=0, max_retries=0, timeout=self.timeout)
                if response.status != 200:
                    raise HttpFailure(f"http_{response.status}", response.status)
                if response.headers.get("grpc-status", "0") != "0":
                    raise ValueError("gRPC failure")
                body = await response.body()
                result = classify(body)
                if is_captcha(grpc_message(body)) and self.captcha_count < 2:
                    self.captcha_pending = True
                    if self.on_captcha is not None:
                        self.on_captcha()
                    # Let the site's own UI run the CAPTCHA flow. Tokens are
                    # neither generated nor solved by Watson. Final results
                    # are withheld so no password/OTP flow can start.
                    await route.fulfill(response=response, body=body)
                    deliver = True
                elif not self.reply.done():
                    self.reply.set_result(result)
            except Exception as error:
                if not self.reply.done():
                    self.reply.set_exception(error)
            finally:
                if not deliver:
                    await route.abort()
            return
        # Background CAPTCHA transport and bounded anonymous initialization.
        host = urlsplit(request.url).hostname
        if (not self.submitted and request.url in INITIAL_REQUESTS
                and self.initial_requests.get(request.url, 0) < INITIAL_REQUESTS[request.url]):
            self.initial_requests[request.url] = self.initial_requests.get(request.url, 0) + 1
            # Session/configuration requests precede identifier submission.
            # Never allow the phone number to be sent in these requests.
            body = request.post_data_buffer or b""
            if self.phone.e164.encode() not in body and national_digits(self.phone).encode() not in body:
                await route.continue_()
                return
        if not self.reply.done() and host in ("www.google.com", "www.recaptcha.net", "recaptcha.net"):
            await route.continue_()
        else:
            await route.abort()


async def browser_lookup(phone, timeout, interactive=False):
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=not interactive)
        guard = None
        try:
            context = await browser.new_context(locale="en-US", service_workers="block")
            page = await context.new_page()
            page.set_default_timeout(timeout * 1000)
            def notify_captcha():
                if interactive:
                    print("Snapchat: complete CAPTCHA in Chrome. Waiting up to 5 minutes; press Ctrl+C to cancel.",
                          file=sys.stderr, flush=True)

            guard = SubmissionGuard(phone, timeout * 1000, notify_captcha)
            response = await page.goto(LOGIN, wait_until="domcontentloaded")
            if response is not None and response.status != 200:
                raise HttpFailure(f"http_{response.status}", response.status)
            # Finish anonymous site initialization before entering any phone.
            # Blocking this initial config traffic prevents the form starting.
            try:
                await page.wait_for_load_state("networkidle", timeout=min(timeout * 1000, 15000))
            except Exception as error:
                if "timeout" not in type(error).__name__.lower():
                    raise
            await context.route("**/*", guard)
            await page.get_by_text("Use phone number instead", exact=True).click()
            await page.locator("main select").first.select_option(phone.region)
            await page.get_by_role("textbox").fill(national_digits(phone))
            await page.get_by_role("button", name="Next", exact=True).click()
            try:
                return await asyncio.wait_for(guard.reply, max(timeout, 300) if interactive else timeout)
            except asyncio.TimeoutError:
                if guard.captcha_pending:
                    return (Status.USER_ACTION_REQUIRED,
                            "Snapchat is waiting for CAPTCHA. Use --snapchat-interactive and complete it in Chrome; registration status is unknown.")
                raise
        finally:
            if guard is not None:
                # Also clean up if initialization/navigation failed before wait.
                if guard.reply.done() and not guard.reply.cancelled():
                    guard.reply.exception()
                elif not guard.reply.done():
                    guard.reply.cancel()
            await browser.close()


class SnapchatAdapter:
    name = "snapchat"
    source_url = LOGIN
    method = "browser_identifier_lookup"
    requirement = "--allow-recovery; Chrome; one lookup with site CAPTCHA. Manual CAPTCHA: --snapchat-interactive; no SMS/OTP."

    async def check(self, phone, http, tokens):
        if not tokens.get("_allow_recovery"):
            return Result(self.name, Status.USER_ACTION_REQUIRED, self.method, self.requirement, source_url=LOGIN)
        try:
            async with http.locks.setdefault(self.name, asyncio.Lock()):
                status, reason = await browser_lookup(phone, http.timeout, bool(tokens.get("_snapchat_interactive")))
            return Result(self.name, status, self.method, reason, source_url=LOGIN)
        except HttpFailure as error:
            return failure_result(self, error)
        except ImportError:
            return Result(self.name, Status.USER_ACTION_REQUIRED, self.method,
                          "Install dependencies: python -m pip install -r requirements.txt",
                          source_url=LOGIN, error_code="missing_browser_dependency")
        except (ValueError, TypeError, KeyError):
            return Result(self.name, Status.UNKNOWN, self.method,
                          "Unrecognized Snapchat response; registration status is unknown.",
                          source_url=LOGIN, error_code="unrecognized_snapchat_response")
        except Exception as error:
            # Includes missing Chrome, a changed form, CAPTCHA and a timeout.
            timeout = "timeout" in type(error).__name__.lower()
            return Result(self.name, Status.USER_ACTION_REQUIRED if not timeout else Status.UNKNOWN,
                          self.method, "Snapchat browser lookup did not finish; registration status is unknown. Check Chrome or open the form manually.",
                          source_url=LOGIN, error_code="snapchat_timeout" if timeout else "snapchat_browser_unavailable")


adapter = SnapchatAdapter()
