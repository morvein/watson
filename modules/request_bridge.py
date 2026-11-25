"""Task-local request and result interface for account-search protocols."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


_request = ContextVar("account_lookup_request")


@contextmanager
def request_scope(request):
    token = _request.set(request)
    try:
        yield
    finally:
        _request.reset(token)


async def impersonate_request_async(url, method="GET", **kwargs):
    return await _request.get()(method, url, **kwargs)


@dataclass
class Result:
    outcome: str
    reason: object = None
    extra: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)

    @classmethod
    def taken(cls, reason=None, **kwargs):
        return cls("found", reason, kwargs.get("extra", {}), kwargs.get("media", {}))

    @classmethod
    def available(cls, reason=None, **kwargs):
        return cls("not_found", reason)

    @classmethod
    def error(cls, reason=None, **kwargs):
        return cls("error", reason)
