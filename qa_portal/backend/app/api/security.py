"""CSRF / origin guard for every write (CONTRACT.md §8). Applies even though
there is no login: the VPN is the perimeter, not proof against a forged
cross-origin request from a browser that IS on the VPN.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Header, Request

from .. import errors
from ..config import settings


def _origin_of(value: str) -> str | None:
    """Normalise a value (an Origin header or a full Referer URL) down to
    "scheme://host:port", lowercased, so prefix tricks like an extra
    subdomain or port can't slip past an allow-list match.
    """
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def csrf_guard(request: Request,
                x_qa_portal: str | None = Header(default=None, alias="X-QA-Portal")) -> None:
    if x_qa_portal != "1":
        raise errors.forbidden("missing X-QA-Portal header on a write request")

    if not settings.allowed_origins:
        return  # no allowlist configured (e.g. local dev/tests) — header check still ran above

    allowed = {_origin_of(o.rstrip("/")) for o in settings.allowed_origins}
    header = request.headers.get("origin") or request.headers.get("referer")
    origin = _origin_of(header) if header else None
    if origin is None or origin not in allowed:
        raise errors.forbidden("Origin/Referer is not on the allowed list")
