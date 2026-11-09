"""RS256 bearer tokens. The development signer cannot be used as production authentication."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import jwt
from jwt import InvalidTokenError

from apex.config import Settings
from apex.errors import DomainError
from apex.identity.jwks import JwksCache, signing_key
from apex.identity.oidc import discover_jwks_uri


class AuthFailure(Exception):
    """A rejected credential. The message is safe to return to a client."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class TokenClaims:
    """Claims that survived signature, issuer, audience, and time checks."""

    def __init__(self, subject: str, issuer: str) -> None:
        self.subject = subject
        self.issuer = issuer


class TokenAuthenticator:
    """Validate bearer tokens. Callers cannot choose the algorithm."""

    def __init__(self, settings: Settings, *, fetcher: Any | None = None) -> None:
        self._settings = settings
        if settings.auth_oidc_discovery and not settings.auth_jwks_url:
            try:
                discovered = (
                    discover_jwks_uri(settings, fetcher)
                    if fetcher is not None
                    else discover_jwks_uri(settings)
                )
            except DomainError:
                discovered = ""
            self._settings = settings.model_copy(update={"auth_jwks_url": discovered})
        self._jwks = JwksCache(self._settings) if self._settings.auth_jwks_url else None

    def configured(self) -> bool:
        verifier = self._settings.auth_jwks_url or self._settings.auth_public_key_pem
        return bool(self._settings.auth_issuer and self._settings.auth_audience and verifier)

    def active(self) -> bool:
        """Production uses the configured issuer. Other environments use it only when enabled."""
        if not self.configured():
            return False
        if self._settings.environment == "production":
            return True
        return self._settings.dev_auth_enabled

    def validate(self, token: str) -> TokenClaims:
        if not self.active():
            raise AuthFailure(
                "authentication_not_configured",
                "This server has no authentication mechanism configured.",
            )
        try:
            key = self._verification_key(token)
            payload: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._settings.auth_audience,
                issuer=self._settings.auth_issuer,
                leeway=0,
                options={
                    "require": ["exp", "iat", "nbf", "iss", "aud", "sub"],
                    "verify_signature": True,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthFailure("token_expired", "The bearer token has expired.") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthFailure("invalid_issuer", "The bearer token issuer is not accepted.") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthFailure(
                "invalid_audience", "The bearer token audience is not accepted."
            ) from exc
        except jwt.ImmatureSignatureError as exc:
            raise AuthFailure("token_not_yet_valid", "The bearer token is not valid yet.") from exc
        except InvalidTokenError as exc:
            raise AuthFailure("invalid_token", "The bearer token is not valid.") from exc
        subject = payload.get("sub")
        issuer = payload.get("iss")
        if not isinstance(subject, str) or not subject or not isinstance(issuer, str):
            raise AuthFailure("invalid_token", "The bearer token is not valid.")
        if subject != subject.strip() or len(subject) > 256:
            raise AuthFailure("invalid_token", "The bearer token is not valid.")
        return TokenClaims(subject=subject, issuer=issuer)

    def _verification_key(self, token: str) -> Any:
        """Use JWKS when it is configured. A JWKS failure does not fall back to a PEM."""
        if self._jwks is not None:
            try:
                return signing_key(token, self._jwks)
            except DomainError as exc:
                raise AuthFailure(exc.code, exc.message) from exc
        return self._settings.auth_public_key_pem


def issue_dev_token(
    settings: Settings,
    subject: str,
    *,
    ttl_seconds: int = 3600,
    now: datetime | None = None,
    issuer: str | None = None,
    audience: str | None = None,
    private_key_pem: str | None = None,
    kid: str | None = None,
) -> str:
    """Sign a development token. Production configuration must not call this."""
    if settings.environment == "production":
        raise AuthFailure(
            "development_auth_rejected",
            "Development tokens cannot be issued when the environment is production.",
        )
    key = private_key_pem if private_key_pem is not None else settings.auth_private_key_pem
    if not key:
        raise AuthFailure(
            "authentication_not_configured",
            "The development signing key is not configured.",
        )
    moment = now or datetime.now(UTC)
    issued = int(moment.timestamp())
    payload = {
        "sub": subject,
        "iss": issuer or settings.auth_issuer,
        "aud": audience or settings.auth_audience,
        "iat": issued,
        "nbf": issued - 1,
        "exp": issued + ttl_seconds,
    }
    if kid:
        token = jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})
    else:
        token = jwt.encode(payload, key, algorithm="RS256")
    if isinstance(token, bytes):
        return token.decode("ascii")
    return token
