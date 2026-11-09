"""Destination checks for JWKS and remote MCP. Token contents cannot choose a URL."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

from apex.config import Settings
from apex.errors import DomainError

Resolver = Callable[[str, int], list[str]]


def validate_remote_url(
    url: str,
    settings: Settings,
    *,
    allow_local_fixture: bool,
    resolver: Resolver | None = None,
) -> str:
    """Return the URL when it satisfies the trust policy. Reject everything else."""
    parts = urlsplit(url)
    if parts.username or parts.password or parts.fragment:
        raise DomainError("endpoint_rejected", "The endpoint URL is not acceptable.")
    host = parts.hostname
    if not host or parts.path is None:
        raise DomainError("endpoint_rejected", "The endpoint URL is not acceptable.")
    port = parts.port
    local_ok = _local_exception(settings, allow_local_fixture=allow_local_fixture)
    if parts.scheme == "https":
        pass
    elif parts.scheme == "http" and local_ok and _literal_loopback(host):
        pass
    else:
        raise DomainError("endpoint_rejected", "The endpoint URL is not acceptable.")
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    addresses = resolve_host(host, port, resolver)
    assert_safe_addresses(addresses, allow_loopback=local_ok)
    return url


def resolve_host(
    host: str,
    port: int,
    resolver: Resolver | None = None,
) -> list[str]:
    """Resolve a host. Tests pass a resolver; production uses the system resolver."""
    if resolver is not None:
        return resolver(host, port)
    if _literal_ip(host) is not None:
        return [host]
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DomainError("endpoint_rejected", "The endpoint name could not be resolved.") from exc
    found: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in found:
            found.append(address)
    if not found:
        raise DomainError("endpoint_rejected", "The endpoint name could not be resolved.")
    return found


def assert_safe_addresses(addresses: list[str], *, allow_loopback: bool) -> None:
    if not addresses:
        raise DomainError("endpoint_rejected", "The endpoint has no address.")
    for address in addresses:
        ip = _literal_ip(address)
        if ip is None or _blocked(ip, allow_loopback=allow_loopback):
            raise DomainError("endpoint_rejected", "The endpoint address is not allowed.")


def fetch_bounded(
    url: str,
    settings: Settings,
    *,
    allow_local_fixture: bool,
    timeout_seconds: float,
    max_bytes: int,
    resolver: Resolver | None = None,
) -> bytes:
    """GET a URL once. Redirects are rejected. The resolved address is checked again."""
    parts = urlsplit(
        validate_remote_url(
            url, settings, allow_local_fixture=allow_local_fixture, resolver=resolver
        )
    )
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    before = resolve_host(host, port, resolver)
    local_ok = _local_exception(settings, allow_local_fixture=allow_local_fixture)
    assert_safe_addresses(before, allow_loopback=local_ok)
    try:
        with httpx.Client(
            timeout=timeout_seconds, follow_redirects=False, trust_env=False
        ) as client:
            response = client.get(url)
    except httpx.TimeoutException as exc:
        raise DomainError("endpoint_timeout", "The endpoint did not respond in time.") from exc
    except httpx.HTTPError as exc:
        raise DomainError("endpoint_rejected", "The endpoint could not be reached.") from exc
    if response.is_redirect or response.status_code in range(300, 400):
        raise DomainError("endpoint_rejected", "The endpoint redirected the request.")
    if response.status_code != 200:
        raise DomainError("endpoint_rejected", "The endpoint rejected the request.")
    body = response.content
    if len(body) > max_bytes:
        raise DomainError("endpoint_rejected", "The endpoint response is too large.")
    after = resolve_host(host, port, resolver)
    if set(after) != set(before):
        raise DomainError("endpoint_rejected", "The endpoint address changed during the request.")
    assert_safe_addresses(after, allow_loopback=local_ok)
    return body


def _local_exception(settings: Settings, *, allow_local_fixture: bool) -> bool:
    return allow_local_fixture and settings.environment in {"development", "test"}


def _literal_loopback(host: str) -> bool:
    ip = _literal_ip(host)
    return ip is not None and ip.is_loopback


def _literal_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_loopback: bool) -> bool:
    if allow_loopback and ip.is_loopback:
        return False
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )
