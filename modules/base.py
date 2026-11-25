from dataclasses import dataclass, field
from typing import Protocol
import asyncio
import json
from urllib.parse import urljoin

import httpx
from scrapling import Selector

from .common import HttpFailure
from .common import Result, Status
from .common import parse_retry_after


class Adapter(Protocol):
    name: str
    source_url: str
    method: str
    requirement: str

    async def check(self, phone, http, tokens) -> Result: ...


@dataclass(frozen=True)
class LookupResult:
    outcome: str
    evidence: str
    extra: dict[str, str | int] = field(default_factory=dict)
    media: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class UnsupportedAdapter:
    name: str
    source_url: str
    reason: str
    method: str = "none"
    requirement: str = "No confirmed automatic method; see the official help page."

    async def check(self, phone, http, tokens):
        return Result(self.name, Status.UNSUPPORTED, self.method, self.reason, source_url=self.source_url)


def failure_result(adapter, error):
    if error.code in ("challenge_required", "additional_verification"):
        return Result(adapter.name, Status.USER_ACTION_REQUIRED, adapter.method,
                      "The service requested CAPTCHA or manual verification. Open its official page.",
                      source_url=adapter.source_url, error_code=error.code)
    if error.status in (401, 403):
        status, reason = Status.AUTH_REQUIRED, "The token expired, was rejected or lacks the required permissions."
    elif error.status == 429:
        status, reason = Status.RATE_LIMITED, "The service rate-limited the request; no automatic retry."
    elif error.status is not None and error.status < 500:
        status, reason = Status.UNKNOWN, "The HTTP response does not confirm whether the identifier is linked."
    else:
        status, reason = Status.ERROR, "Could not obtain a reliable API response; link status is unknown."
    return Result(adapter.name, status, adapter.method, reason, source_url=adapter.source_url,
                  retry_after=error.retry_after, error_code=error.code)


def html_page(text):
    # Adaptive storage is disabled: no local selector database or cookies on disk.
    return Selector(text, adaptive=False)


def first(page, selector):
    items = page.css(selector)
    return items[0] if items else None


def check_challenge(page):
    if page.css('input[name="cvf_captcha_input"], input#captchacharacters, form[action*="validateCaptcha"], iframe[src*="recaptcha"], iframe[src*="hcaptcha"], #captcha, #challenge-running'):
        raise HttpFailure("challenge_required")


class RecoverySession:
    """Cookie-aware identifier lookup. One POST maximum, never retried."""
    def __init__(self, http, service, hosts, max_posts=1):
        self.http, self.service, self.hosts = http, service, set(hosts)
        self.posts = 0
        self.max_posts = max_posts

    async def request(self, method, url, **kwargs):
        read_redirect_paths = kwargs.pop("read_redirect_paths", ())
        accepted_statuses = kwargs.pop("accepted_statuses", (200,))
        for redirects in range(5):
            parsed = httpx.URL(url)
            if parsed.scheme != "https" or parsed.host not in self.hosts or parsed.userinfo:
                raise HttpFailure("unexpected_destination")
            if method == "POST":
                if self.posts >= self.max_posts:
                    raise HttpFailure("duplicate_submission_blocked")
                self.posts += 1
            await self.http.sleep(max(0, self.http.next_at.get(self.service, 0) - self.http.clock()))
            self.http.next_at[self.service] = self.http.clock() + self.http.min_interval
            try:
                async with self.http.client.stream(method, url, follow_redirects=False, **kwargs) as response:
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 1024 * 1024:
                            raise HttpFailure("response_too_large")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    # aiter_bytes already decodes gzip/br; do not decode it again.
                    headers = response.headers.copy()
                    headers.pop("content-encoding", None)
                    headers.pop("content-length", None)
                    result = httpx.Response(response.status_code, headers=headers, content=body, request=response.request)
            except httpx.DecodingError:
                raise HttpFailure("invalid_response_encoding") from None
            except httpx.TimeoutException:
                raise HttpFailure("recovery_timeout") from None
            except httpx.HTTPError:
                raise HttpFailure("recovery_network_error") from None
            retry_after = parse_retry_after(result.headers.get("retry-after"))
            if retry_after is not None:
                self.http.next_at[self.service] = max(self.http.next_at[self.service], self.http.clock() + retry_after)
            if result.status_code == 429:
                raise HttpFailure("http_429", 429, retry_after)
            if method == "POST" and result.status_code in (301, 302, 303) and result.headers.get("location"):
                destination = httpx.URL(urljoin(str(result.url), result.headers["location"]))
                if (destination.scheme != "https" or destination.host != parsed.host or destination.userinfo
                        or destination.path not in read_redirect_paths):
                    raise HttpFailure("additional_verification")
                # Read the result page. Never repeat or redirect the POST body.
                method, url = "GET", str(destination)
                kwargs = {"headers": kwargs.get("headers", {})}
                continue
            if method == "GET" and result.status_code in (301, 302, 303, 307, 308) and result.headers.get("location"):
                url = urljoin(str(result.url), result.headers["location"])
                kwargs.pop("params", None)
                continue
            if result.status_code not in accepted_statuses:
                raise HttpFailure(f"http_{result.status_code}", result.status_code, retry_after)
            return result
        raise HttpFailure("redirect_limit")

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self.request("POST", url, **kwargs)


class RecoveryAdapter:
    automatic = True
    max_posts = 1
    method = "recovery_lookup"
    requirement = "--allow-recovery; one identifier POST, no automatic retry; service notifications are possible."

    async def check(self, phone, http, tokens):
        if not tokens.get("_allow_recovery"):
            return Result(self.name, Status.USER_ACTION_REQUIRED, self.method, self.requirement, source_url=self.source_url)
        try:
            async with http.locks.setdefault(self.name, asyncio.Lock()):
                session = RecoverySession(http, self.name, self.hosts, self.max_posts)
                lookup = await self.lookup(phone.e164, session)
            if not isinstance(lookup, LookupResult):
                lookup = LookupResult(*lookup)
            outcome, evidence = lookup.outcome, lookup.evidence
            if outcome == "found":
                return Result(self.name, Status.LINKED, self.method,
                              getattr(self, "positive_reason", "The service recognized the identifier at this step; number ownership was not verified."),
                              evidence=[evidence], source_url=self.source_url,
                              extra=lookup.extra, media=lookup.media)
            if outcome == "not_found":
                return Result(self.name, Status.NOT_LINKED, self.method,
                              getattr(self, "negative_reason", "The service explicitly did not find the identifier at this step; other link types were not checked."),
                              evidence=[evidence], source_url=self.source_url)
            raise ValueError("Unrecognized lookup outcome")
        except HttpFailure as error:
            return failure_result(self, error)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return Result(self.name, Status.UNKNOWN, self.method,
                          "Unrecognized response or markup; registration status is unknown.",
                          source_url=self.source_url, error_code="unrecognized_recovery_response")
