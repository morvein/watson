from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


class Status(StrEnum):
    LINKED = "linked"
    NOT_LINKED = "not_linked"
    UNKNOWN = "unknown"
    AUTH_REQUIRED = "auth_required"
    USER_ACTION_REQUIRED = "user_action_required"
    RATE_LIMITED = "rate_limited"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Result:
    service: str
    status: Status
    method: str
    reason: str
    evidence: list[str] = field(default_factory=list)
    checked_at: str = field(default_factory=utc_now)
    duration_ms: int = 0
    source_url: str = ""
    retry_after: float | None = None
    error_code: str | None = None
    extra: dict[str, str | int] = field(default_factory=dict)
    media: dict[str, str] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


from dataclasses import asdict, dataclass

import phonenumbers as pn


@dataclass(frozen=True)
class Phone:
    e164: str
    national: str
    country_code: int
    region: str | None
    possible: bool
    valid: bool
    number_type: str

    @property
    def masked(self):
        return self.e164[:2] + "*" * max(0, len(self.e164) - 4) + self.e164[-2:]

    def to_dict(self):
        return asdict(self)


def normalize(raw, region=None):
    raw = raw.strip()
    if not raw or any(c in raw for c in "\r\n\0"):
        raise ValueError("Enter one phone number.")
    if region and region.upper() not in pn.SUPPORTED_REGIONS:
        raise ValueError("Unknown ISO region code; use KZ, RU or another supported region.")
    if not raw.startswith("+") and not region:
        raise ValueError("For a number without +, specify --region (for example KZ or RU).")
    try:
        parsed = pn.parse(raw, region.upper() if region else None)
    except pn.NumberParseException:
        raise ValueError("Could not parse the phone number.") from None
    if parsed.extension:
        raise ValueError("Phone extensions are not supported.")
    kind = pn.number_type(parsed)
    kind_name = next((name for name, value in vars(pn.PhoneNumberType).items() if value == kind and name.isupper()), "UNKNOWN")
    return Phone(pn.format_number(parsed, pn.PhoneNumberFormat.E164),
                 pn.format_number(parsed, pn.PhoneNumberFormat.NATIONAL), parsed.country_code,
                 pn.region_code_for_number(parsed), pn.is_possible_number(parsed),
                 pn.is_valid_number(parsed), kind_name)


def matches(candidate, phone):
    # Never guess a region for an API value that lacks an international prefix.
    if not isinstance(candidate, str) or not candidate.strip().startswith("+"):
        return False
    try:
        return normalize(candidate).e164 == phone.e164
    except ValueError:
        return False


"""Read-only HTTP transport; mutations and recovery requests are not supported."""
import asyncio
import json
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx


class HttpFailure(Exception):
    def __init__(self, code, status=None, retry_after=None):
        super().__init__(code)
        self.code, self.status, self.retry_after = code, status, retry_after


def parse_retry_after(value, now=None):
    if not value:
        return None
    try:
        if value.strip().isdigit():
            return float(value.strip())
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0.0, (date - (now or datetime.now(timezone.utc))).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


class HttpClient:
    def __init__(self, *, client=None, timeout=30, min_interval=1, retries=2, sleep=asyncio.sleep, clock=time.monotonic, jitter=random.random):
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=5),
            follow_redirects=False, trust_env=False,
        )
        self.owned = client is None
        self.timeout, self.min_interval, self.retries = timeout, min_interval, retries
        self.sleep, self.clock, self.jitter = sleep, clock, jitter
        self.locks, self.next_at = {}, {}

    async def close(self):
        if self.owned:
            await self.client.aclose()

    async def get_json(self, service, url, *, token, params=None):
        try:
            async with asyncio.timeout(self.timeout):
                # Lock gives each service its own FIFO queue including retry waits.
                async with self.locks.setdefault(service, asyncio.Lock()):
                    return await self._get(service, url, token, params)
        except TimeoutError:
            raise HttpFailure("deadline_exceeded") from None

    async def _get(self, service, url, token, params):
        for attempt in range(self.retries + 1):
            await self.sleep(max(0, self.next_at.get(service, 0) - self.clock()))
            self.next_at[service] = self.clock() + self.min_interval
            try:
                async with self.client.stream("GET", url, headers={"Authorization": f"Bearer {token}"}, params=params) as response:
                    retry_after = parse_retry_after(response.headers.get("retry-after"))
                    if retry_after is not None:
                        self.next_at[service] = max(self.next_at[service], self.clock() + retry_after)
                    if response.status_code == 429:
                        raise HttpFailure("http_429", 429, retry_after)
                    if response.status_code in (502, 503, 504) and attempt < self.retries:
                        await self.sleep(max(retry_after or 0, min(8, 2 ** attempt + self.jitter())))
                        continue
                    if response.status_code != 200:
                        raise HttpFailure(f"http_{response.status_code}", response.status_code, retry_after)
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type != "application/json" and not content_type.endswith("+json"):
                        raise HttpFailure("unexpected_content_type")
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 1024 * 1024:
                            raise HttpFailure("response_too_large")
                        chunks.append(chunk)
                    try:
                        data = json.loads(b"".join(chunks))
                    except (ValueError, UnicodeError):
                        raise HttpFailure("invalid_json") from None
                    if not isinstance(data, dict):
                        raise HttpFailure("invalid_schema")
                    return data
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt == self.retries:
                    raise HttpFailure("network_timeout" if isinstance(error, httpx.TimeoutException) else "network_error") from None
                await self.sleep(min(8, 2 ** attempt + self.jitter()))
            except httpx.HTTPError:
                raise HttpFailure("http_transport_error") from None


import json
import os
import tempfile
from pathlib import Path



def result_fields(result):
    return {key: value for key, value in {**result.extra, **result.media}.items()
            if key not in {"email_lookup_status", "metadata_error"} and value is not None and value != ""}


def compact_report(results, *, interrupted=False):
    labels = {Status.LINKED: "registered", Status.NOT_LINKED: "not registered",
              Status.RATE_LIMITED: "rate limit", Status.USER_ACTION_REQUIRED: "action required",
              Status.AUTH_REQUIRED: "auth required", Status.UNSUPPORTED: "unavailable"}
    payload = {}
    for result in sorted(results, key=lambda item: item.service):
        item = {"status": labels.get(result.status, str(result.status))}
        if result.status == Status.LINKED:
            item.update(result_fields(result))
        if result.error_code:
            item["error"] = result.error_code
        payload[result.service] = item
    if interrupted:
        payload["interrupted"] = True
    return payload


def export_report(path, phone, results, *, interrupted=False, demo=False):
    payload = compact_report(results, interrupted=interrupted)
    destination = Path(path).resolve()
    # Atomic replacement leaves a valid report if interrupted during writing.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False) as stream:
        temporary = stream.name
        try:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            os.unlink(temporary)
            raise
    try:
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


import asyncio
import time
from dataclasses import replace



async def run_checks(phone, adapters, http, tokens, *, timeout=None, output=None, on_result=lambda result: None):
    """Wait for every service; apply a shared deadline only when requested."""
    results = []
    interrupted = False
    deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout

    async def check(adapter):
        before = time.monotonic()
        try:
            async with asyncio.timeout_at(deadline):
                result = await adapter.check(phone, http, tokens)
        except TimeoutError:
            result = Result(adapter.name, Status.ERROR, adapter.method,
                            "The service did not respond before the timeout.",
                            source_url=adapter.source_url, error_code="deadline_exceeded")
        except Exception:
            result = Result(adapter.name, Status.ERROR, adapter.method,
                            "Internal adapter error; result is unconfirmed.",
                            source_url=adapter.source_url, error_code="adapter_error")
        return replace(result, duration_ms=round((time.monotonic() - before) * 1000))

    tasks = [asyncio.create_task(check(adapter)) for adapter in adapters]
    try:
        for completed in asyncio.as_completed(tasks):
            result = await completed
            results.append(result)
            if output:
                export_report(output, phone, results)
            on_result(result)
    except (asyncio.CancelledError, KeyboardInterrupt):
        interrupted = True
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if output:
            export_report(output, phone, results, interrupted=interrupted)
    return results, interrupted
