"""Cookie-preserving browser HTTP sessions for bounded identifier lookups."""
import asyncio

import httpx

from .base import LookupResult, failure_result
from .common import HttpFailure, Result, Status, parse_retry_after


def browser_session(timeout, profile="chrome120"):
    from curl_cffi.requests import AsyncSession
    return AsyncSession(impersonate=profile, timeout=timeout, allow_redirects=False)


class LookupSession:
    def __init__(self, raw, adapter):
        self.raw, self.adapter = raw, adapter
        self.posts = 0

    async def request(self, method, url, **kwargs):
        for _ in range(5):
            parsed = httpx.URL(url)
            if (parsed.scheme != "https" or parsed.userinfo or parsed.host not in self.adapter.hosts
                    or not self.adapter.allowed(method, parsed.path)):
                raise HttpFailure("unexpected_destination")
            if method == "POST":
                self.posts += 1
                if self.posts > self.adapter.max_posts:
                    raise HttpFailure("duplicate_submission_blocked")
            try:
                response = await self.raw.request(method, str(parsed), allow_redirects=False, **kwargs)
            except Exception as error:
                code = "web_lookup_timeout" if "timeout" in type(error).__name__.lower() else "web_lookup_network_error"
                raise HttpFailure(code) from None
            if len(response.content) > 2 * 1024 * 1024:
                raise HttpFailure("response_too_large")
            status = response.status_code
            retry = parse_retry_after(response.headers.get("retry-after"))
            if method == "GET" and status in (301, 302, 303, 307, 308) and response.headers.get("location"):
                url = str(parsed.join(response.headers["location"]))
                kwargs.pop("params", None)
                continue
            if status != 200:
                raise HttpFailure(f"http_{status}", status, retry)
            return response
        raise HttpFailure("redirect_limit")

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self.request("POST", url, **kwargs)


class WebLookupAdapter:
    method = "web_identifier_lookup"
    requirement = "--allow-recovery; identifier lookup before code delivery, no automatic retries."
    max_posts = 1

    async def check(self, phone, http, tokens):
        if not tokens.get("_allow_recovery"):
            return Result(self.name, Status.USER_ACTION_REQUIRED, self.method, self.requirement,
                          source_url=self.source_url)
        try:
            async with http.locks.setdefault(self.name, asyncio.Lock()):
                async with browser_session(timeout=http.timeout) as raw:
                    lookup = await self.lookup(phone, LookupSession(raw, self))
            if not isinstance(lookup, LookupResult) or lookup.outcome not in ("found", "not_found", "unknown", "action_required"):
                raise ValueError("Invalid lookup outcome")
            if lookup.outcome in ("unknown", "action_required"):
                status = Status.USER_ACTION_REQUIRED if lookup.outcome == "action_required" else Status.UNKNOWN
                return Result(self.name, status,
                              self.method, lookup.evidence, source_url=self.source_url,
                              error_code=f"{self.name}_{lookup.outcome}")
            return Result(self.name, Status.LINKED if lookup.outcome == "found" else Status.NOT_LINKED,
                          self.method, "The service returned an explicit identifier lookup result.",
                          evidence=[lookup.evidence], source_url=self.source_url,
                          extra=lookup.extra if lookup.outcome == "found" else {},
                          media=lookup.media if lookup.outcome == "found" else {})
        except ImportError:
            return Result(self.name, Status.ERROR, self.method,
                          "Install dependencies: python -m pip install -r requirements.txt",
                          source_url=self.source_url, error_code="missing_curl_cffi")
        except HttpFailure as error:
            return failure_result(self, error)
        except (ValueError, TypeError, KeyError, AttributeError):
            return Result(self.name, Status.UNKNOWN, self.method,
                          "Unrecognized web lookup response; registration status is unknown.",
                          source_url=self.source_url, error_code="unrecognized_lookup_response")
