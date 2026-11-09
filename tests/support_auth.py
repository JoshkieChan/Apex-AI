"""Local RS256 issuer for tests. This is not a production identity provider."""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from apex.config import Settings
from apex.identity.tokens import issue_dev_token

ISSUER = "https://apex.test/issuer"
AUDIENCE = "apex-api"

_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _PRIVATE.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("ascii")
PUBLIC_PEM = (
    _PRIVATE.public_key()
    .public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode("ascii")
)


def auth_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "dev_auth_enabled": True,
        "auth_issuer": ISSUER,
        "auth_audience": AUDIENCE,
        "auth_public_key_pem": PUBLIC_PEM,
        "auth_private_key_pem": PRIVATE_PEM,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def bearer(
    principal: str = "dev-user",
    idempotency_key: str | None = None,
    *,
    ttl_seconds: int = 3600,
) -> dict[str, str]:
    token = issue_dev_token(auth_settings(), principal, ttl_seconds=ttl_seconds)
    headers = {"Authorization": f"Bearer {token}"}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers
