"""OK.ru account lookup; attribution: THIRD_PARTY_NOTICES.md."""
import json
import re
import httpx
from bs4 import BeautifulSoup

from .base import LookupResult, RecoveryAdapter, check_challenge, html_page
from .metadata import clean_text, public_url


OK_LOGIN_URL = "https://ok.ru/"
OK_LOOKUP_URL = "https://ok.ru/web-api/v2/login/getRecoveryUrl"
OK_RECOVER_URL = "https://ok.ru/dk?st.cmd=anonymRecoveryStart"


def parse_recovery_url(data):
    if not isinstance(data, dict) or data.get("success") is not True or not isinstance(data.get("result"), str):
        raise ValueError("OK.ru did not return a successful recovery lookup")
    destination = public_url(data["result"], OK_LOGIN_URL)
    if not destination:
        raise ValueError("Invalid OK.ru recovery destination")
    url = httpx.URL(destination)
    commands = url.params.get_list("st.cmd")
    if (url.host not in ("ok.ru", "www.ok.ru") or url.path != "/dk" or len(commands) != 1
            or commands[0] not in ("anonymRecoveryStart", "anonymRecoveryAfterFailedLogin")
            or set(url.params) - {"st.cmd", "st._aid", "st.lit"}):
        raise ValueError("Unexpected OK.ru recovery action")
    return str(url), commands[0]


def parse_recovery_response(html):
    page = html_page(html)
    check_challenge(page)
    found = bool(page.css('div[data-l="registrationContainer,offer_contact_rest"]'))
    start = bool(page.css('div[data-l="registrationContainer,home_rest"]'))
    if found and not start:
        return "found"
    if start and not found:
        # Also shown when a lookup never ran: this alone is not a negative result.
        return "unknown"
    raise ValueError("Unrecognized or conflicting OK.ru recovery containers")


def parse_profile_details(html):
    root = BeautifulSoup(html, "html.parser").select_one(
        'div[data-l="registrationContainer,offer_contact_rest"]')
    if root is None:
        return {}, {}
    extra, media = {}, {}

    def text(selector):
        tag = root.select_one(selector)
        return clean_text(tag.get_text(" ", strip=True)) if tag else ""

    for key, selector in (
        ("masked_name", ".ext-registration_username_header"),
        ("masked_email", '[data-l="t,email"] .ext-registration_stub_small_header'),
        ("masked_phone", '[data-l="t,phone"] .ext-registration_stub_small_header'),
    ):
        if value := text(selector):
            extra[key] = value
    info = root.select_one(".ext-registration_tx.taCenter")
    if info:
        lines = [clean_text(tag.get_text(" ", strip=True)) for tag in info.select(".lstp-t")]
        for line in filter(None, lines):
            created = re.match(r"(?:Профиль создан|Profile created)\s+(.+)", line, re.I)
            if created:
                extra["registered_at"] = created[1]
            elif "profile_info" not in extra:
                extra["profile_info"] = line
        image = info.find("img", src=True)
        if image and (avatar := public_url(image.get("src"), OK_LOGIN_URL)):
            media["avatar"] = avatar
        profile = info.select_one('a[href^="/profile/"]')
        if profile and (url := public_url(profile.get("href"), OK_LOGIN_URL)):
            extra["profile_url"] = url
            match = re.fullmatch(r"/profile/(\d+)/?", httpx.URL(url).path)
            if match:
                extra["user_id"] = match[1]
    return extra, media


class OdnoklassnikiAdapter(RecoveryAdapter):
    name = "odnoklassniki"
    method = "recovery_identifier"
    source_url = OK_RECOVER_URL
    hosts = ("www.ok.ru", "ok.ru")
    requirement = "--allow-recovery; profile lookup before selecting a code delivery method."

    async def lookup(self, target, client):
        headers = {"Accept-Language": "en-US,en;q=0.9"}
        await client.get(OK_LOGIN_URL, headers=headers)
        response = await client.post(OK_LOOKUP_URL, content=json.dumps({
            "id": 1,
            "parameters": {"login": target, "loginIntentToken": None, "location": "home.login_form"},
        }), headers={**headers, "Content-Type": "text/plain;charset=UTF-8",
                     "Origin": "https://ok.ru", "Referer": OK_LOGIN_URL, "Ok-Screen": "anonymMain"})
        destination, command = parse_recovery_url(response.json())
        response = await client.get(destination, headers=headers)
        outcome = parse_recovery_response(response.text)
        if outcome == "found" and command == "anonymRecoveryAfterFailedLogin":
            extra, media = parse_profile_details(response.text)
            return LookupResult("found", "OK.ru getRecoveryUrl lookup + offer_contact_rest profile.", extra, media)
        if command == "anonymRecoveryStart" and outcome == "unknown":
            return LookupResult("not_found", "OK.ru getRecoveryUrl returned recovery start; home_rest confirmed.")
        raise ValueError("OK.ru lost lookup context; the start page is not a negative result")


adapter = OdnoklassnikiAdapter()
