"""JWKS verification against a local fixture. No external identity provider is required."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from tests.support_auth import AUDIENCE, ISSUER, PRIVATE_PEM, PUBLIC_PEM

from apex.api.app import create_app
from apex.config import Settings
from apex.identity.tokens import AuthFailure, TokenAuthenticator, issue_dev_token

_KID = "fixture-key"


class _JwksServer(ThreadingHTTPServer):
    body = b"{}"
    delay = 0.0
    status = 200
    location = ""
    requests = 0


def _handler_factory() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            server = self.server
            assert isinstance(server, _JwksServer)
            server.requests += 1
            if server.delay:
                import time

                time.sleep(server.delay)
            if server.status == 302:
                self.send_response(302)
                self.send_header("Location", server.location)
                self.end_headers()
                return
            self.send_response(server.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(server.body)))
            self.end_headers()
            self.wfile.write(server.body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def _serve(body: bytes) -> tuple[_JwksServer, str]:
    server = _JwksServer(("127.0.0.1", 0), _handler_factory())
    server.body = body
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}/jwks"


def _jwk(public_key: Any, kid: str) -> dict[str, Any]:
    document = json.loads(RSAAlgorithm.to_jwk(public_key))
    document["kid"] = kid
    document["alg"] = "RS256"
    document["use"] = "sig"
    return document


def _settings(url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "dev_auth_enabled": True,
        "auth_issuer": ISSUER,
        "auth_audience": AUDIENCE,
        "auth_jwks_url": url,
        "auth_jwks_allow_http_fixture": True,
        "auth_jwks_refresh_seconds": 60,
        "auth_jwks_cache_seconds": 300,
        "auth_jwks_timeout_seconds": 1,
        "auth_jwks_max_bytes": 65536,
        "model_provider": "mock",
        "database_url": "sqlite+pysqlite:///:memory:",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_jwks_accepts_a_local_token_and_rejects_bad_claims() -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        encoding=_pem_encoding(),
        format=_pem_format(),
        encryption_algorithm=_pem_encryption(),
    ).decode("ascii")
    document = json.dumps({"keys": [_jwk(private.public_key(), _KID)]}).encode()
    server, url = _serve(document)
    try:
        settings = _settings(url, auth_private_key_pem=pem)
        authenticator = TokenAuthenticator(settings)
        token = issue_dev_token(settings, "dev-user", kid=_KID)
        assert authenticator.validate(token).subject == "dev-user"
        assert server.requests == 1
        assert authenticator.validate(token).subject == "dev-user"
        assert server.requests == 1

        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_pem = other.private_bytes(
            encoding=_pem_encoding(),
            format=_pem_format(),
            encryption_algorithm=_pem_encryption(),
        ).decode("ascii")
        forged = issue_dev_token(settings, "dev-user", private_key_pem=other_pem, kid=_KID)
        with pytest.raises(AuthFailure):
            authenticator.validate(forged)
        with pytest.raises(AuthFailure) as wrong_issuer:
            authenticator.validate(
                issue_dev_token(settings, "dev-user", issuer="https://other", kid=_KID)
            )
        assert wrong_issuer.value.code == "invalid_issuer"
        with pytest.raises(AuthFailure) as wrong_audience:
            authenticator.validate(
                issue_dev_token(settings, "dev-user", audience="other", kid=_KID)
            )
        assert wrong_audience.value.code == "invalid_audience"
        with pytest.raises(AuthFailure) as expired:
            authenticator.validate(
                issue_dev_token(
                    settings,
                    "dev-user",
                    now=datetime.now(UTC) - timedelta(hours=2),
                    ttl_seconds=60,
                    kid=_KID,
                )
            )
        assert expired.value.code == "token_expired"
        future = issue_dev_token(
            settings,
            "dev-user",
            now=datetime.now(UTC) + timedelta(hours=1),
            kid=_KID,
        )
        with pytest.raises(AuthFailure) as not_yet:
            authenticator.validate(future)
        assert not_yet.value.code in {"token_not_yet_valid", "invalid_token"}
        unsigned = jwt.encode(
            {"sub": "dev-user", "iss": ISSUER, "aud": AUDIENCE, "iat": 1, "nbf": 1, "exp": 2},
            "not-the-rsa-key-and-long-enough-for-hmac",
            algorithm="HS256",
        )
        with pytest.raises(AuthFailure):
            authenticator.validate(unsigned)
        unknown = issue_dev_token(settings, "dev-user", kid="missing")
        before = server.requests
        with pytest.raises(AuthFailure):
            authenticator.validate(unknown)
        assert server.requests == before + 1
        with pytest.raises(AuthFailure):
            authenticator.validate(issue_dev_token(settings, "dev-user", kid="missing-again"))
        assert server.requests == before + 1
    finally:
        server.shutdown()


def test_key_rotation_and_removed_keys() -> None:
    first = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    second = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    first_pem = _private_pem(first)
    second_pem = _private_pem(second)
    server, url = _serve(json.dumps({"keys": [_jwk(first.public_key(), "old")]}).encode())
    try:
        settings = _settings(url, auth_jwks_refresh_seconds=0, auth_private_key_pem=first_pem)
        authenticator = TokenAuthenticator(settings)
        old = issue_dev_token(settings, "dev-user", kid="old")
        assert authenticator.validate(old).subject == "dev-user"
        server.body = json.dumps({"keys": [_jwk(second.public_key(), "new")]}).encode()
        rotated = issue_dev_token(settings, "dev-user", private_key_pem=second_pem, kid="new")
        assert authenticator.validate(rotated).subject == "dev-user"
        with pytest.raises(AuthFailure):
            authenticator.validate(old)
    finally:
        server.shutdown()


def test_malformed_timeout_size_and_redirect() -> None:
    server, url = _serve(b"not-json")
    try:
        with pytest.raises(AuthFailure):
            TokenAuthenticator(_settings(url)).validate(_sample())
        server.body = b'{"keys": []}'
        TokenAuthenticator(_settings(url, auth_jwks_refresh_seconds=0))
        with pytest.raises(AuthFailure):
            TokenAuthenticator(_settings(url, auth_jwks_refresh_seconds=0)).validate(_sample())
        server.status = 302
        server.location = "http://127.0.0.1/elsewhere"
        with pytest.raises(AuthFailure):
            TokenAuthenticator(_settings(url, auth_jwks_refresh_seconds=0)).validate(_sample())
        server.status = 200
        server.location = ""
        server.body = b'{"keys": ' + b"x" * 200
        with pytest.raises(AuthFailure):
            TokenAuthenticator(
                _settings(url, auth_jwks_refresh_seconds=0, auth_jwks_max_bytes=64)
            ).validate(_sample())
        server.delay = 1.5
        server.body = b"{}"
        with pytest.raises(AuthFailure):
            TokenAuthenticator(
                _settings(url, auth_jwks_refresh_seconds=0, auth_jwks_timeout_seconds=0.2)
            ).validate(_sample())
    finally:
        server.shutdown()


def test_private_address_and_production_do_not_fall_back(engine: Engine) -> None:
    with pytest.raises(AuthFailure):
        TokenAuthenticator(
            _settings("https://10.9.8.7/jwks", auth_jwks_allow_http_fixture=False)
        ).validate(_sample())
    try:
        create_app(
            settings=_settings(
                "http://127.0.0.1/jwks",
                environment="production",
                dev_auth_enabled=False,
                auth_jwks_allow_http_fixture=False,
                auth_public_key_pem=PUBLIC_PEM,
            ),
            engine=engine,
        )
    except RuntimeError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("production accepted an HTTP JWKS URL")

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server, url = _serve(json.dumps({"keys": [_jwk(private.public_key(), "other")]}).encode())
    try:
        settings = _settings(
            url,
            environment="production",
            dev_auth_enabled=False,
            auth_public_key_pem=PUBLIC_PEM,
            auth_private_key_pem="",
            seed_dev_principals=False,
        )
        with Session(engine) as db:
            db.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"
                )
            )
            db.commit()
        authenticator = TokenAuthenticator(settings)
        pem_token = issue_dev_token(
            _settings(url, environment="test", auth_private_key_pem=PRIVATE_PEM),
            "dev-user",
            kid="pem-key",
        )
        with pytest.raises(AuthFailure):
            authenticator.validate(pem_token)
    finally:
        server.shutdown()


def _sample() -> str:
    return issue_dev_token(
        _settings("http://127.0.0.1/jwks", auth_private_key_pem=PRIVATE_PEM),
        "dev-user",
        kid=_KID,
    )


def _private_pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        encoding=_pem_encoding(),
        format=_pem_format(),
        encryption_algorithm=_pem_encryption(),
    ).decode("ascii")


def _pem_encoding() -> Any:
    from cryptography.hazmat.primitives import serialization

    return serialization.Encoding.PEM


def _pem_format() -> Any:
    from cryptography.hazmat.primitives import serialization

    return serialization.PrivateFormat.PKCS8


def _pem_encryption() -> Any:
    from cryptography.hazmat.primitives import serialization

    return serialization.NoEncryption()
