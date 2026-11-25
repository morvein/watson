"""User-supplied recovery search; private document ID may change."""
import json
import html
import re
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from .base import LookupResult, RecoveryAdapter, check_challenge, first, html_page
from .metadata import clean_text, public_url
from .common import HttpFailure

LOGIN_URL = "https://www.facebook.com/login/identify/"
GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
QUERY_NAME = "CAAFBAccountSearchViewQuery"
DOCUMENT_ID = "28496659306608697"
MASKED_EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+\-]*\*[a-zA-Z0-9_*.+\-]*@[a-zA-Z0-9_*.+\-]+")


def masked_emails(text):
    # Never expand masked characters returned by the recovery page.
    return list(dict.fromkeys(MASKED_EMAIL_REGEX.findall(html.unescape(text))))


def recovery_metadata_url(value):
    url = public_url(value, LOGIN_URL)
    if not url:
        return ""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    if any(re.search(r"send|code|verify|confirm|resend|dispatch|deliver", key, re.I) for key in query):
        return ""
    return url if parts.hostname == "www.facebook.com" and parts.path == "/recover/init/" else ""


def parse_search_tokens(html):
    page = html_page(html)
    check_challenge(page)
    element = first(page, "script#__eqmc")
    token, values = None, []
    if element is not None:
        try:
            data = json.loads(element.text)
            if isinstance(data, dict):
                token = data.get("l")
                if isinstance(data.get("u"), str):
                    values = parse_qs(urlsplit(data["u"]).query).get("jazoest", [])
        except ValueError:
            pass
    if not isinstance(token, str) or not token:
        field = first(page, 'input[name="lsd"]')
        token = field.attrib.get("value") if field is not None else None
    if len(values) != 1 or not values[0].isdigit():
        field = first(page, 'input[name="jazoest"]')
        values = [field.attrib.get("value", "")] if field is not None else []
    if not isinstance(token, str) or not token or len(values) != 1 or not values[0].isdigit():
        raise ValueError("Invalid form token")
    return token, values[0]


def decode_response(text):
    stripped = text.lstrip()
    if stripped.startswith("for (;;);"):
        stripped = stripped[len("for (;;);"):]
    return json.loads(stripped)


def parse_search_response(body):
    if not isinstance(body, dict) or body.get("errors"):
        raise ValueError("Account search returned an error")
    data = body.get("data")
    result = data.get("caa_ar_fb_account_search") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise ValueError("Unrecognized account search response")
    count, accounts = result.get("num_results_shown"), result.get("accounts")
    if type(count) is not int or count < 0 or not isinstance(accounts, list):
        raise ValueError("Invalid result schema")
    if count != len(accounts) or any(not isinstance(account, dict) or not account for account in accounts):
        raise ValueError("Inconsistent results")
    return "found" if count else "not_found"


class FacebookAdapter(RecoveryAdapter):
    name = "facebook"
    source_url = LOGIN_URL
    hosts = ("www.facebook.com",)

    async def lookup(self, target, client):
        response = await client.get(LOGIN_URL)
        token, jazoest = parse_search_tokens(response.text)
        variables = {"params": {"cipher_text": None, "context": "recover", "event_request_id": str(uuid4()),
                    "friend_name": "", "search_query": target, "waterfall_id": str(uuid4())}}
        response = await client.post(GRAPHQL_URL, data={
            "fb_api_caller_class": "RelayModern", "fb_api_req_friendly_name": QUERY_NAME,
            "doc_id": DOCUMENT_ID, "server_timestamps": "true", "lsd": token, "jazoest": jazoest,
            "__user": "0", "__a": "1", "variables": json.dumps(variables, separators=(",", ":")),
        }, headers={"X-FB-Friendly-Name": QUERY_NAME, "X-FB-LSD": token, "X-ASBD-ID": "359341",
                    "Origin": "https://www.facebook.com", "Referer": LOGIN_URL})
        data = decode_response(response.text)
        outcome = parse_search_response(data)
        count = data["data"]["caa_ar_fb_account_search"]["num_results_shown"]
        extra, media = {}, {}
        if outcome == "found":
            search = data["data"]["caa_ar_fb_account_search"]
            account = search["accounts"][0]
            name = clean_text(account.get("name"))
            if name and name != target:
                extra["name"] = name
            avatar = public_url(account.get("profile_pic_url"))
            if avatar:
                media["avatar"] = avatar
            if count > 1:
                extra["matched_accounts"] = count
            # Read the returned recovery page. Do not select a
            # delivery channel or submit its code-sending forms.
            recovery = recovery_metadata_url(search.get("redirect_uri"))
            if recovery:
                try:
                    response = await client.get(recovery)
                    emails = masked_emails(response.text)
                    if emails:
                        extra["masked_email"] = ", ".join(emails)
                except HttpFailure as error:
                    extra["metadata_error"] = error.code
        return LookupResult(outcome,
                            f"Facebook account-search: num_results_shown={count}; validated count/accounts schema.",
                            extra, media)


adapter = FacebookAdapter()
