"""Cancelable HTTP transport for the Instagram request sequence."""
from .common import HttpFailure, parse_retry_after
from .instagram_protocol import GRAPHQL_URL, HOME_PAGE_URL, SEARCH_FRIENDLY_NAME, TIMELOCK_FRIENDLY_NAME


def browser_session(timeout):
    # Lazy import keeps methods/dry-run usable before installing the dependency.
    from curl_cffi.requests import AsyncSession
    return AsyncSession(impersonate="chrome", timeout=timeout, allow_redirects=False)


class InstagramRequests:
    def __init__(self, client):
        self.client = client
        self.failure = None
        self.stage = "home"
        self.search_payload = None
        self.requests = 0

    async def request(self, method, url, **kwargs):
        self.requests += 1
        expected = (("GET", HOME_PAGE_URL), ("POST", GRAPHQL_URL), ("POST", GRAPHQL_URL))
        if self.requests > 3 or (method, url) != expected[self.requests - 1]:
            self.failure = HttpFailure("instagram_unexpected_request")
            raise self.failure
        self.stage = ("home", "timelock", "search")[self.requests - 1]
        if method == "POST":
            friendly = kwargs.get("data", {}).get("fb_api_req_friendly_name")
            wanted = TIMELOCK_FRIENDLY_NAME if self.stage == "timelock" else SEARCH_FRIENDLY_NAME
            if friendly != wanted:
                self.failure = HttpFailure("instagram_unexpected_query")
                raise self.failure
        try:
            response = await self.client.request(method, url, **kwargs)
        except Exception as error:
            # Cancellation is a BaseException and propagates to the session's
            # async context manager, which closes the actual pending curl handle.
            name = type(error).__name__.lower()
            self.failure = HttpFailure(f"instagram_{self.stage}_timeout" if "timeout" in name
                                       else f"instagram_{self.stage}_network_error")
            raise self.failure from None
        if response.status_code != 200:
            self.failure = HttpFailure(f"http_{response.status_code}", response.status_code,
                                       parse_retry_after(response.headers.get("retry-after")))
            return response
        if self.stage == "home":
            return response
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            if self.stage == "search":
                self.search_payload = payload
            else:
                challenge = payload.get("data", {}).get("caa_account_recovery_content", {}).get("lox_pow_challenge")
                if challenge:
                    self.validate_challenge(challenge)
        except (ValueError, TypeError, AttributeError, KeyError):
            self.failure = HttpFailure(f"instagram_{self.stage}_invalid_response")
            raise self.failure from None
        return response

    @staticmethod
    def validate_challenge(challenge):
        if not isinstance(challenge, dict):
            raise ValueError("Invalid challenge")
        iterations = challenge.get("iterations", 2000)
        if type(iterations) is not int or not 0 <= iterations <= 100000:
            raise ValueError("Challenge iteration limit")
        for key in ("base", "modulus"):
            value = challenge.get(key)
            if not isinstance(value, str) or not value or len(value) > 1024:
                raise ValueError("Invalid challenge size")
            number = int(value, 16)
            if number < 0 or (key == "modulus" and number < 2):
                raise ValueError("Invalid challenge number")
        if not all(isinstance(challenge.get(key), str) and challenge[key] for key in ("mac", "token")):
            raise ValueError("Incomplete challenge")
