"""OpenID Connect discovery for one configured issuer. Tokens cannot choose the URL."""

from __future__ import annotations

import json
from collections.abc import Callable

from apex.config import Settings
from apex.errors import DomainError
from apex.netpolicy import fetch_bounded

Fetcher = Callable[..., bytes]


def discover_jwks_uri(settings: Settings, fetcher: Fetcher = fetch_bounded) -> str:
    """Read jwks_uri from the configured issuer. A token claim cannot replace it."""
    issuer = settings.auth_issuer.rstrip("/")
    if not issuer:
        raise DomainError("invalid_token", "The bearer token is not valid.")
    document_url = f"{issuer}/.well-known/openid-configuration"
    body = fetcher(
        document_url,
        settings,
        allow_local_fixture=settings.auth_jwks_allow_http_fixture,
        timeout_seconds=settings.auth_jwks_timeout_seconds,
        max_bytes=settings.auth_jwks_max_bytes,
    )
    try:
        document = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DomainError("invalid_token", "The bearer token is not valid.") from exc
    if not isinstance(document, dict):
        raise DomainError("invalid_token", "The bearer token is not valid.")
    if document.get("issuer") != settings.auth_issuer:
        raise DomainError("invalid_token", "The bearer token is not valid.")
    jwks_uri = document.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise DomainError("invalid_token", "The bearer token is not valid.")
    fetcher(
        jwks_uri,
        settings,
        allow_local_fixture=settings.auth_jwks_allow_http_fixture,
        timeout_seconds=settings.auth_jwks_timeout_seconds,
        max_bytes=settings.auth_jwks_max_bytes,
    )
    return jwks_uri
