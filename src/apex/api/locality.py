"""Local-peer checks for development-only routes. Forwarding headers are not evidence."""

from __future__ import annotations

from fastapi import Request

from apex.config import Settings

_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


_PROXY_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "forwarded",
    "x-real-ip",
    "via",
    "x-original-url",
    "cf-connecting-ip",
    "x-forwarded-server",
)


def proxy_indicated(request: Request) -> bool:
    """True when a proxy header is present. Those headers are not a locality proof."""
    return any(request.headers.get(header) for header in _PROXY_HEADERS)


def local_peer(request: Request, settings: Settings) -> bool:
    """True only for an immediate loopback peer, or the test client in the test environment."""
    client = request.client
    host = client.host if client is not None else ""
    if host in _LOOPBACK:
        return True
    return settings.environment == "test" and host == "testclient"
