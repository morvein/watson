"""Zoho phone lookup; attribution: THIRD_PARTY_NOTICES.md."""
import time
from urllib.parse import urlsplit

from .base import LookupResult
from .common import HttpFailure
from .metadata import clean_text
from .web_lookup import WebLookupAdapter


HOSTS = tuple("accounts.zoho." + suffix for suffix in ("com", "eu", "in", "com.au", "jp", "com.cn", "ca", "sa"))


def datacenter_url(value):
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        valid = url.scheme == "https" and url.hostname in HOSTS and not url.username and not url.password and url.port in (None, 443)
    except ValueError:
        return None
    if not valid:
        return None
    return f"https://{url.hostname}"


def parse_lookup(data):
    if not isinstance(data, dict):
        raise ValueError("Expected Zoho JSON object")
    errors = data.get("errors", [])
    if not isinstance(errors, list) or any(not isinstance(error, dict) for error in errors):
        raise ValueError("Invalid Zoho errors")
    codes = {error.get("code") for error in errors if isinstance(error.get("code"), str)}
    status = data.get("status_code")
    message = str(data.get("message", ""))
    found = status == 201 or message == "User exists" or bool(codes & {"U200", "U400", "U409"})
    found = found or "User exists in another DC" in message or "User is marked as spam" in message
    absent = "U401" in codes or (status == 400 and "does not exist" in message.lower())
    if found == absent or "IN106" in codes or message == "Invalid input":
        raise ValueError("Unknown, invalid, or conflicting Zoho result")
    if absent:
        return LookupResult("not_found", "Zoho: explicit user-not-found response."), None
    extra = {}
    lookup = data.get("lookup", {})
    if isinstance(lookup, dict):
        identifier = lookup.get("identifier")
        if isinstance(identifier, (str, int)) and str(identifier):
            extra["user_id"] = str(identifier)
        modes = lookup.get("modes", {})
        if isinstance(modes, dict) and isinstance(modes.get("allowed_modes"), list):
            values = [clean_text(mode) for mode in modes["allowed_modes"] if isinstance(mode, str)]
            if values:
                extra["allowed_modes"] = ", ".join(values)
    if "U409" in codes or "User is marked as spam" in message:
        extra["account_status"] = "inactive_or_spam"
    regional = "U400" in codes or "User exists in another DC" in message
    destination = None
    if regional:
        body = data.get("data")
        destination = datacenter_url(body.get("redirect_uri")) if isinstance(body, dict) else None
        if destination:
            extra["datacenter"] = urlsplit(destination).hostname
        else:
            extra["metadata_error"] = "invalid_datacenter_destination"
    return LookupResult("found", "Zoho: explicit user-exists response.", extra), destination


class ZohoAdapter(WebLookupAdapter):
    name = "zoho"
    source_url = "https://accounts.zoho.com/signin"
    hosts = HOSTS
    max_posts = 2

    def allowed(self, method, path):
        return (method == "GET" and path in ("/signin", "/signin/")) or (method == "POST" and path.startswith("/signin/v2/lookup/"))

    async def lookup_at(self, phone, session, base):
        headers = {"Accept": "*/*", "Accept-Language": "en-US,en;q=0.9", "Origin": base,
                   "Referer": base + "/", "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}
        initial = await session.get(base + "/signin", headers=headers)
        csrf = initial.cookies.get("iamcsr")
        if not csrf:
            raise ValueError("Zoho CSRF cookie missing")
        headers["X-ZCSRF-TOKEN"] = f"iamcsrcoo={csrf}"
        country = str(phone.country_code)
        target = country + "-" + phone.e164[1 + len(country):]
        response = await session.post(base + "/signin/v2/lookup/" + target, headers=headers, data={
            "mode": "primary", "cli_time": str(int(time.time() * 1000)),
            "servicename": "AaaServer", "serviceurl": base + "/u/h",
        })
        return parse_lookup(response.json())

    async def lookup(self, phone, session):
        lookup, regional = await self.lookup_at(phone, session, "https://accounts.zoho.com")
        if regional and regional != "https://accounts.zoho.com":
            try:
                details, _ = await self.lookup_at(phone, session, regional)
                if details.outcome == "found":
                    lookup.extra.update(details.extra)
                else:
                    lookup.extra["metadata_error"] = "regional_result_conflict"
            except HttpFailure as error:
                lookup.extra["metadata_error"] = error.code
            except (ValueError, TypeError, KeyError, AttributeError):
                lookup.extra["metadata_error"] = "unrecognized_regional_response"
        return lookup


adapter = ZohoAdapter()
