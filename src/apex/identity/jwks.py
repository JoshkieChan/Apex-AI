"""RS256 verification against a configured JWKS document. The token cannot choose the URL."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import jwt
from jwt import PyJWK

from apex.config import Settings
from apex.errors import DomainError
from apex.netpolicy import fetch_bounded

_MAX_KEYS = 16


class JwksCache:
    """Cache a configured key set. An unknown kid may refresh once, not forever."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = 0.0
        self._last_attempt = 0.0
        self._last_unknown_refresh = float("-inf")

    def key_for(self, kid: str, *, now: float | None = None) -> PyJWK:
        moment = time.monotonic() if now is None else now
        with self._lock:
            current = self._current(moment)
            if kid in current:
                return current[kid]
            initial = not self._keys
            if (
                not initial
                and moment - self._last_unknown_refresh < self._settings.auth_jwks_refresh_seconds
            ):
                raise DomainError("invalid_token", "The bearer token is not valid.")
            if not initial:
                self._last_unknown_refresh = moment
            self._refresh(moment)
            current = self._current(moment)
            if kid not in current:
                self._last_unknown_refresh = moment
                raise DomainError("invalid_token", "The bearer token is not valid.")
            return current[kid]

    def _current(self, moment: float) -> dict[str, PyJWK]:
        if not self._keys:
            return {}
        if moment - self._fetched_at > self._settings.auth_jwks_cache_seconds:
            return {}
        return self._keys

    def _refresh(self, moment: float) -> None:
        self._last_attempt = moment
        fresh = bool(self._keys) and (
            moment - self._fetched_at <= self._settings.auth_jwks_cache_seconds
        )
        try:
            body = fetch_bounded(
                self._settings.auth_jwks_url,
                self._settings,
                allow_local_fixture=self._settings.auth_jwks_allow_http_fixture,
                timeout_seconds=self._settings.auth_jwks_timeout_seconds,
                max_bytes=self._settings.auth_jwks_max_bytes,
            )
            self._keys = _parse_jwks(body)
            self._fetched_at = moment
        except DomainError:
            if not fresh:
                self._keys = {}
                self._fetched_at = 0.0


def _parse_jwks(body: bytes) -> dict[str, PyJWK]:
    try:
        document = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DomainError("invalid_token", "The bearer token is not valid.") from exc
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list) or not keys or len(keys) > _MAX_KEYS:
        raise DomainError("invalid_token", "The bearer token is not valid.")
    parsed: dict[str, PyJWK] = {}
    for item in keys:
        if not isinstance(item, dict):
            raise DomainError("invalid_token", "The bearer token is not valid.")
        if (
            item.get("kty") != "RSA"
            or item.get("alg", "RS256") != "RS256"
            or item.get("use", "sig") != "sig"
        ):
            raise DomainError("invalid_token", "The bearer token is not valid.")
        kid = item.get("kid")
        if not isinstance(kid, str) or not kid or kid in parsed:
            raise DomainError("invalid_token", "The bearer token is not valid.")
        try:
            parsed[kid] = PyJWK.from_dict(item)
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise DomainError("invalid_token", "The bearer token is not valid.") from exc
    return parsed


def signing_key(token: str, cache: JwksCache) -> Any:
    """Select the pinned RS256 key named by the token kid."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise DomainError("invalid_token", "The bearer token is not valid.") from exc
    if header.get("alg") != "RS256":
        raise DomainError("invalid_token", "The bearer token is not valid.")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise DomainError("invalid_token", "The bearer token is not valid.")
    if "jku" in header or "x5u" in header or "jwk" in header:
        raise DomainError("invalid_token", "The bearer token is not valid.")
    return cache.key_for(kid).key
