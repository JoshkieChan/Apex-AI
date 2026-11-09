"""Development token issuance is local, explicit, and absent from production."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from tests.support_auth import AUDIENCE, ISSUER, PRIVATE_PEM, PUBLIC_PEM

from apex.api.app import create_app
from apex.config import Settings


def test_issuance_requires_a_local_bootstrap(engine: Engine) -> None:
    application = create_app(settings=_settings(dev_token_issuance=True), engine=engine)
    with TestClient(application) as local:
        missing = local.post("/v1/dev/tokens", json={"subject": "dev-user"})
        assert missing.status_code == 404
        minted = local.post(
            "/v1/dev/tokens",
            headers={"X-Apex-Dev-Bootstrap": "local-bootstrap-value"},
            json={"subject": "dev-user"},
        )
        assert minted.status_code == 200
        assert minted.json()["token_type"] == "Bearer"
        assert minted.json()["access_token"]

    remote = TestClient(application, client=("10.2.3.4", 5000))
    rejected = remote.post(
        "/v1/dev/tokens",
        headers={
            "X-Apex-Dev-Bootstrap": "local-bootstrap-value",
            "X-Forwarded-For": "127.0.0.1",
        },
        json={"subject": "dev-user"},
    )
    assert rejected.status_code == 404
    forwarded = TestClient(application, client=("10.2.3.4", 5000))
    via = forwarded.post(
        "/v1/dev/tokens",
        headers={
            "X-Apex-Dev-Bootstrap": "local-bootstrap-value",
            "Via": "1.1 shared-proxy",
        },
        json={"subject": "dev-user"},
    )
    assert via.status_code == 404


def test_loopback_with_a_proxy_header_cannot_mint(engine: Engine) -> None:
    application = create_app(settings=_settings(dev_token_issuance=True), engine=engine)
    with TestClient(application, client=("127.0.0.1", 9)) as local:
        response = local.post(
            "/v1/dev/tokens",
            headers={
                "X-Apex-Dev-Bootstrap": "local-bootstrap-value",
                "X-Forwarded-For": "127.0.0.1",
            },
            json={"subject": "dev-user"},
        )
    assert response.status_code == 404


def test_issuance_is_off_unless_explicitly_enabled(engine: Engine) -> None:
    application = create_app(settings=_settings(dev_token_issuance=False), engine=engine)
    with TestClient(application) as local:
        response = local.post(
            "/v1/dev/tokens",
            headers={"X-Apex-Dev-Bootstrap": "local-bootstrap-value"},
            json={"subject": "dev-user"},
        )
    assert response.status_code == 404


def test_production_rejects_issuance_configuration(engine: Engine) -> None:
    try:
        create_app(
            settings=_settings(
                environment="production",
                dev_auth_enabled=False,
                seed_dev_principals=False,
                auth_private_key_pem="",
                dev_token_issuance=True,
            ),
            engine=engine,
        )
    except RuntimeError as exc:
        assert "issuance" in str(exc)
    else:
        raise AssertionError("production accepted development token issuance")


def test_invalid_signing_configuration_fails_closed(engine: Engine) -> None:
    application = create_app(
        settings=_settings(dev_token_issuance=True, auth_private_key_pem=""),
        engine=engine,
    )
    with TestClient(application) as local:
        response = local.post(
            "/v1/dev/tokens",
            headers={"X-Apex-Dev-Bootstrap": "local-bootstrap-value"},
            json={"subject": "dev-user"},
        )
    assert response.status_code == 401
    assert response.json()["error"] == "authentication_not_configured"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "dev_auth_enabled": True,
        "dev_token_issuance": False,
        "dev_token_bootstrap": "local-bootstrap-value",
        "auth_issuer": ISSUER,
        "auth_audience": AUDIENCE,
        "auth_public_key_pem": PUBLIC_PEM,
        "auth_private_key_pem": PRIVATE_PEM,
        "seed_dev_principals": True,
        "database_url": "sqlite+pysqlite:///:memory:",
        "model_provider": "mock",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]
