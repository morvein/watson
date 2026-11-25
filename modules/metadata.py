"""Small helpers for fields explicitly returned by a service."""
from urllib.parse import urljoin, urlsplit


def clean_text(value):
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:2000]


def public_url(value, base=""):
    if not isinstance(value, str) or not value.strip():
        return ""
    value = urljoin(base, value.strip())
    try:
        parts = urlsplit(value)
        if parts.scheme == "https" and parts.hostname and not parts.username and not parts.password:
            return value
    except ValueError:
        pass
    return ""
