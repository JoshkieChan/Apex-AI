"""Server-verified identity. Development headers are not credentials."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from tests.conftest import auth
from tests.support_auth import AUDIENCE, ISSUER, PRIVATE_PEM, auth_settings

from apex.api.app import create_app
from apex.config import Settings
from apex.identity.tokens import issue_dev_token

ANALYZE = "Analyze the following text and report its word count: hi"


def test_missing_invalid_and_impersonated_credentials(client: TestClient) -> None:
    missing = client.post("/v1/tasks", json={"request": ANALYZE})
    assert missing.status_code == 401
    assert missing.json()["error"] == "missing_credentials"
    assert "BEGIN" not in missing.text

    expired = client.post(
        "/v1/tasks",
        headers={"Authorization": f"Bearer {_token(ttl_seconds=-30)}"},
        json={"request": ANALYZE},
    )
    assert expired.status_code == 401
    assert expired.json()["error"] == "token_expired"

    immature = client.post(
        "/v1/tasks",
        headers={"Authorization": f"Bearer {_token(now=datetime.now(UTC) + timedelta(hours=2))}"},
        json={"request": ANALYZE},
    )
    assert immature.status_code == 401
    assert immature.json()["error"] == "token_not_yet_valid"

    wrong_issuer = client.post(
        "/v1/tasks",
        headers={"Authorization": f"Bearer {_token(issuer='https://other.example')}"},
        json={"request": ANALYZE},
    )
    assert wrong_issuer.status_code == 401
    assert wrong_issuer.json()["error"] == "invalid_issuer"

    wrong_audience = client.post(
        "/v1/tasks",
        headers={"Authorization": f"Bearer {_token(audience='other-api')}"},
        json={"request": ANALYZE},
    )
    assert wrong_audience.status_code == 401
    assert wrong_audience.json()["error"] == "invalid_audience"

    forged = client.post(
        "/v1/tasks",
        headers={"Authorization": f"Bearer {_token(private_key_pem=_other_private_key())}"},
        json={"request": ANALYZE},
    )
    assert forged.status_code == 401
    assert forged.json()["error"] == "invalid_token"

    unsupported = client.post(
        "/v1/tasks",
        headers={"Authorization": f"Bearer {_hmac_token()}"},
        json={"request": ANALYZE},
    )
    assert unsupported.status_code == 401
    assert unsupported.json()["error"] == "invalid_token"

    unsigned = client.post(
        "/v1/tasks",
        headers={"Authorization": f"Bearer {_unsigned_token()}"},
        json={"request": ANALYZE},
    )
    assert unsigned.status_code == 401
    assert unsigned.json()["error"] == "invalid_token"
    assert PRIVATE_PEM not in unsigned.text

    swapped = client.get(
        "/v1/principal",
        headers={**auth("dev-user"), "X-Apex-Principal": "dev-approver"},
    )
    assert swapped.status_code == 200
    assert swapped.json()["subject"] == "dev-user"

    query = client.get("/v1/principal", headers=auth(), params={"principal_id": "dev-approver"})
    assert query.status_code == 400
    assert query.json()["error"] == "impersonation_rejected"


def test_production_rejects_the_development_header_and_is_not_ready(engine: Engine) -> None:
    unconfigured = Settings(
        _env_file=None,
        environment="production",
        dev_auth_enabled=False,
        seed_dev_principals=False,
        model_provider="mock",
        database_url="sqlite+pysqlite:///:memory:",
    )
    application = create_app(settings=unconfigured, engine=engine)
    with TestClient(application) as production:
        health = production.get("/health")
        ready = production.get("/ready")
        denied = production.post("/v1/tasks", json={"request": ANALYZE})
        header = production.post(
            "/v1/tasks",
            headers={"X-Apex-Principal": "dev-user"},
            json={"request": ANALYZE},
        )
        minted = production.post("/v1/dev/tokens", json={"subject": "dev-user"})
    assert health.status_code == 200
    assert health.json()["auth_mode"] == "unconfigured"
    assert ready.status_code == 503
    assert denied.status_code == 401
    assert denied.json()["error"] == "authentication_not_configured"
    assert header.status_code == 401
    assert header.json()["error"] == "development_auth_rejected"
    assert minted.status_code == 404

    configured = Settings(
        _env_file=None,
        environment="production",
        dev_auth_enabled=False,
        seed_dev_principals=False,
        model_provider="mock",
        auth_issuer=ISSUER,
        auth_audience=AUDIENCE,
        auth_public_key_pem=auth_settings().auth_public_key_pem,
        database_url="sqlite+pysqlite:///:memory:",
    )
    secured = create_app(settings=configured, engine=engine)
    with Session(engine) as db:
        db.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        db.execute(text("DELETE FROM alembic_version"))
        db.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0007_interaction')"))
        db.commit()
    with TestClient(secured) as live:
        assert live.get("/ready").status_code == 200
        assert live.get("/health").json()["auth_mode"] == "bearer"
        rejected = live.post(
            "/v1/tasks",
            headers={"X-Apex-Principal": "dev-user"},
            json={"request": ANALYZE},
        )
    assert rejected.status_code == 401
    assert rejected.json()["error"] == "development_auth_rejected"


def test_partial_production_auth_refuses_to_start(engine: Engine) -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        auth_issuer=ISSUER,
        model_provider="mock",
        database_url="sqlite+pysqlite:///:memory:",
    )
    try:
        create_app(settings=settings, engine=engine)
    except RuntimeError as exc:
        assert "incomplete" in str(exc)
    else:
        raise AssertionError("partial production authentication started")


def _token(**overrides: object) -> str:
    settings = auth_settings()
    subject = str(overrides.pop("subject", "dev-user"))
    return issue_dev_token(settings, subject, **overrides)  # type: ignore[arg-type]


def _other_private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _hmac_token() -> str:
    now = int(datetime.now(UTC).timestamp())
    return jwt.encode(
        {
            "sub": "dev-user",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "nbf": now - 1,
            "exp": now + 60,
        },
        "not-the-rsa-key-and-long-enough-for-hmac",
        algorithm="HS256",
    )


def _unsigned_token() -> str:
    now = int(datetime.now(UTC).timestamp())
    header = _b64({"alg": "none", "typ": "JWT"})
    payload = _b64(
        {
            "sub": "dev-user",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "nbf": now - 1,
            "exp": now + 60,
        }
    )
    return f"{header}.{payload}."


def _b64(value: dict[str, object]) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
