"""Secret references. The database stores the reference, never the value."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Protocol

import httpx

from apex.config import Settings
from apex.errors import DomainError

_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_VAULT_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,127}$")


def validate_secret_reference(reference: str) -> str:
    """Accept env:, file:, or vault: references and reject a raw secret."""
    kind, _, name = reference.partition(":")
    if kind == "env" and _NAME.fullmatch(name):
        return reference
    if kind == "file" and _NAME.fullmatch(name):
        return reference
    if kind == "vault" and _VAULT_PATH.fullmatch(name) and ".." not in name:
        return reference
    raise DomainError("invalid_secret_reference", "The credential reference is not valid.")


class SecretProvider(Protocol):
    """Resolve a reference. Implementations must not log the returned value."""

    name: str

    def resolve(self, reference: str) -> str:
        """Return the secret for a validated reference."""

    def ready(self) -> bool:
        """True when the provider can be used. This does not reveal a secret."""


class EnvSecretProvider:
    """Local and test provider. Production refuses it when an external provider is required."""

    name = "env"

    def resolve(self, reference: str) -> str:
        validate_secret_reference(reference)
        if not reference.startswith("env:"):
            raise DomainError("secret_unavailable", "The referenced credential is not available.")
        value = os.environ.get(reference.removeprefix("env:"), "")
        if not value:
            raise DomainError("secret_unavailable", "The referenced credential is not available.")
        return value

    def ready(self) -> bool:
        return True


class FileSecretProvider:
    """Read mounted secret files. The directory is outside the database."""

    name = "file"

    def __init__(self, directory: str, cache_seconds: int) -> None:
        self._directory = Path(directory)
        self._cache_seconds = cache_seconds
        self._cache: dict[str, tuple[str, float]] = {}

    def resolve(self, reference: str) -> str:
        validate_secret_reference(reference)
        if not reference.startswith("file:"):
            raise DomainError("secret_unavailable", "The referenced credential is not available.")
        name = reference.removeprefix("file:")
        cached = self._cache.get(name)
        now = time.monotonic()
        if cached is not None and now - cached[1] < self._cache_seconds:
            return cached[0]
        path = (self._directory / name).resolve()
        root = self._directory.resolve()
        if root not in path.parents:
            raise DomainError("invalid_secret_reference", "The credential reference is not valid.")
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise DomainError(
                "secret_unavailable", "The referenced credential is not available."
            ) from exc
        if not value:
            raise DomainError("secret_unavailable", "The referenced credential is not available.")
        self._cache[name] = (value, now)
        return value

    def ready(self) -> bool:
        return self._directory.is_dir()


class VaultSecretProvider:
    """HashiCorp Vault KV v2. The token is read from a named environment variable."""

    name = "vault"

    def __init__(
        self,
        address: str,
        token_env: str,
        mount: str,
        cache_seconds: int,
        client: httpx.Client | None = None,
    ) -> None:
        self._address = address.rstrip("/")
        self._token_env = token_env
        self._mount = mount
        self._cache_seconds = cache_seconds
        self._cache: dict[str, tuple[str, float]] = {}
        self._client = client or httpx.Client(timeout=2.0, follow_redirects=False, trust_env=False)

    def resolve(self, reference: str) -> str:
        validate_secret_reference(reference)
        if not reference.startswith("vault:"):
            raise DomainError("secret_unavailable", "The referenced credential is not available.")
        path = reference.removeprefix("vault:")
        cached = self._cache.get(path)
        now = time.monotonic()
        if cached is not None and now - cached[1] < self._cache_seconds:
            return cached[0]
        token = os.environ.get(self._token_env, "")
        if not token or not self._address:
            raise DomainError("secret_unavailable", "The referenced credential is not available.")
        url = f"{self._address}/v1/{self._mount}/data/{path}"
        try:
            response = self._client.get(url, headers={"X-Vault-Token": token})
        except httpx.HTTPError as exc:
            raise DomainError(
                "secret_unavailable", "The referenced credential is not available."
            ) from exc
        if response.status_code != 200:
            raise DomainError("secret_unavailable", "The referenced credential is not available.")
        try:
            value = str(response.json()["data"]["data"]["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError(
                "secret_unavailable", "The referenced credential is not available."
            ) from exc
        if not value:
            raise DomainError("secret_unavailable", "The referenced credential is not available.")
        self._cache[path] = (value, now)
        return value

    def ready(self) -> bool:
        return bool(self._address and os.environ.get(self._token_env, ""))


def build_secret_provider(settings: Settings, client: httpx.Client | None = None) -> SecretProvider:
    """Return the configured provider. Production does not fall back to env."""
    if settings.secret_provider == "file":  # noqa: S105
        return FileSecretProvider(settings.secret_dir, settings.secret_cache_seconds)
    if settings.secret_provider == "vault":  # noqa: S105
        return VaultSecretProvider(
            settings.vault_addr,
            settings.vault_token_env,
            settings.vault_mount,
            settings.secret_cache_seconds,
            client,
        )
    if settings.require_external_secrets:
        raise DomainError(
            "secret_unavailable",
            "An external secret provider is required and is not configured.",
        )
    return EnvSecretProvider()


def resolve_configured_secret(reference: str, settings: Settings) -> str:
    """Resolve a reference through the process secret provider."""
    return build_secret_provider(settings).resolve(reference)
