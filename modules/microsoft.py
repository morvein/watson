"""User-supplied credential-type parser; no third-party OAuth client used."""
import json
import re
import httpx
from .base import LookupResult, RecoveryAdapter, check_challenge, first, html_page
from .common import HttpFailure
from .metadata import clean_text

LOGIN_URL = "https://login.live.com/"


def parse_login_context(html):
    page = html_page(html)
    check_challenge(page)
    match = re.search(r"\b(?:var\s+)?ServerData\s*=\s*", html)
    if match is None:
        raise ValueError("Consumer login context unavailable")
    config, _ = json.JSONDecoder().raw_decode(html, match.end())
    if not isinstance(config, dict) or not isinstance(config.get("urlGetCredentialType"), str):
        raise ValueError("Invalid login context")
    url = httpx.URL(config["urlGetCredentialType"])
    if url.scheme != "https" or url.host != "login.live.com" or url.path != "/GetCredentialType.srf" or url.userinfo:
        raise ValueError("Unexpected credential endpoint")
    tag = config.get("sFTTag")
    if not isinstance(tag, str):
        raise ValueError("Flow token unavailable")
    field = first(html_page(tag), 'input[name="PPFT"]')
    token = field.attrib.get("value") if field is not None else None
    session = config.get("sUnauthSessionID")
    if not isinstance(token, str) or not token or not isinstance(session, str) or not session:
        raise ValueError("Incomplete consumer login context")
    return str(url), token, session


def parse_credential_response(data):
    if not isinstance(data, dict) or data.get("error") or data.get("ErrorCode") or data.get("errorCode"):
        raise ValueError("Credential lookup rejected")
    throttle = data.get("ThrottleStatus", 0)
    if type(throttle) is not int:
        raise ValueError("Invalid throttle status")
    if throttle:
        raise HttpFailure("credential_lookup_throttled", 429)
    value = data.get("IfExistsResult")
    if type(value) is not int or value not in (0, 1):
        raise ValueError("Unrecognized credential status")
    return "found" if value == 0 else "not_found"


def parse_display_details(data, target):
    """Display is a sign-in alias, not a person's name or proof of an email."""
    display = clean_text(data.get("Display"))
    extra = {}
    if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", display):
        key = "masked_email" if any(char in display for char in "*•…") else "email"
        extra[key] = display
    else:
        # The service can echo the supplied phone with spaces or parentheses.
        # Do not present that echo as an account name.
        phone_display = bool(re.fullmatch(r"[+\d\s().-]+", display))
        if display and display != target and not phone_display:
            extra["sign_in_alias"] = display
    return extra


class MicrosoftAdapter(RecoveryAdapter):
    name = "microsoft"
    method = "login_identifier"
    source_url = LOGIN_URL
    hosts = ("login.live.com", "account.live.com")

    async def lookup(self, target, client):
        response = await client.get(LOGIN_URL)
        endpoint, token, session = parse_login_context(response.text)
        response = await client.post(endpoint, json={
            "checkPhones": True, "country": "", "federationFlags": 3, "flowToken": token,
            "isReactLoginRequest": True, "isRemoteNGCSupported": True, "uaid": session,
            "username": target, "forceotclogin": False, "otclogindisallowed": True, "isSignup": False,
        })
        data = response.json()
        outcome = parse_credential_response(data)
        extra = parse_display_details(data, target) if outcome == "found" else {}
        return LookupResult(outcome,
                            f"Microsoft credential lookup: IfExistsResult={data['IfExistsResult']}; ThrottleStatus={data.get('ThrottleStatus', 0)}; sign-in alias context.",
                            extra)


adapter = MicrosoftAdapter()
