"""Instagram account lookup; attribution: THIRD_PARTY_NOTICES.md."""
import asyncio
from dataclasses import replace

from .base import failure_result
from .common import HttpFailure, Result, Status, matches
from .instagram_protocol import validate_instagram
from .instagram_web import InstagramRequests, browser_session
from .metadata import clean_text, public_url
from .request_bridge import request_scope


class InstagramAdapter:
    name = "instagram"
    method = "web_caa_lookup"
    source_url = "https://www.instagram.com/accounts/password/reset/"
    requirement = "--allow-recovery; account lookup without sending a code."

    async def check(self, phone, http, tokens):
        if not tokens.get("_allow_recovery"):
            return Result(self.name, Status.USER_ACTION_REQUIRED, self.method, self.requirement,
                          source_url=self.source_url)
        requests = None
        try:
            async with http.locks.setdefault(self.name, asyncio.Lock()):
                async with browser_session(timeout=http.timeout) as browser:
                    requests = InstagramRequests(browser)
                    with request_scope(requests.request):
                        lookup = await validate_instagram(phone)
            if requests.failure:
                raise requests.failure
            if lookup.outcome == "error":
                return self.unknown(phone, requests.stage, lookup.reason)
            payload = requests.search_payload
            search = payload.get("data", {}).get("caa_ar_ig_account_search") if isinstance(payload, dict) else None
            if (not isinstance(search, dict) or not isinstance(search.get("contact_points"), list)
                    or not isinstance(search.get("profiles", []), list) or payload.get("errors")):
                return self.unknown(phone, "search", "Incomplete search result structure.")
            extra = {}
            media = {}
            if lookup.outcome == "found":
                for key, value in lookup.extra.items():
                    if text := clean_text(value):
                        if key == "name" and matches(text, phone):
                            continue
                        extra[key] = text.replace(phone.e164, phone.masked)
                for key, value in lookup.media.items():
                    if url := public_url(value):
                        media[key] = url
            return Result(self.name, Status.LINKED if lookup.outcome == "found" else Status.NOT_LINKED,
                          self.method, "Account lookup result from the recovery flow.",
                          evidence=["CAAIGAccountSearchViewQuery: validated contact_points array."],
                          source_url=self.source_url, extra=extra, media=media)
        except ImportError:
            return Result(self.name, Status.ERROR, self.method,
                          "Install dependencies: python -m pip install -r requirements.txt",
                          source_url=self.source_url, error_code="missing_curl_cffi")
        except HttpFailure as error:
            result = failure_result(self, error)
            stage = requests.stage if requests else "session"
            detail = f"HTTP {error.status}" if error.status else error.code
            if error.status in (401, 403):
                return replace(result, status=Status.AUTH_REQUIRED,
                               reason=f"Instagram, step {stage}: {detail}. The service rejected the anonymous web request.")
            return replace(result, reason=f"Instagram, step {stage}: {detail}. {result.reason}")
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            return self.unknown(phone, requests.stage if requests else "session", type(error).__name__)

    def unknown(self, phone, stage, reason):
        reason = clean_text(str(reason)).replace(phone.e164, phone.masked)
        return Result(self.name, Status.UNKNOWN, self.method,
                      f"Instagram, step {stage}: {reason}", source_url=self.source_url,
                      error_code=f"instagram_{stage}_unrecognized_response")


adapter = InstagramAdapter()
