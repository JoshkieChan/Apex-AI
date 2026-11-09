"""Deployment Compose must require a password and stay off public interfaces."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "infrastructure" / "docker" / "compose.deploy.yml"
ACCEPTANCE = ROOT / "infrastructure" / "docker" / "compose.acceptance.yml"
DEPLOYMENT_DOC = ROOT / "docs" / "architecture" / "deployment.md"


def test_compose_files_do_not_embed_a_database_password() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")
    acceptance = ACCEPTANCE.read_text(encoding="utf-8")
    for text in (deploy, acceptance):
        assert "POSTGRES_PASSWORD: apex" not in text
        assert "apex:apex@" not in text
        assert "${APEX_POSTGRES_PASSWORD:?APEX_POSTGRES_PASSWORD is required}" in text
        assert "postgresql+psycopg://apex:${APEX_POSTGRES_PASSWORD}@postgres:5432/apex" in text
    assert "127.0.0.1:8000:8000" in deploy
    assert "5433:5432" not in deploy
    assert "8001:8001" not in deploy
    assert deploy.count('command: ["apex-worker"]') == 1
    assert "127.0.0.1:18080:8000" in acceptance
    assert "127.0.0.1:55433:5432" in acceptance
    assert "18081:8001" not in acceptance
    assert "0.0.0.0:" not in deploy
    assert "0.0.0.0:" not in acceptance
    assert "Horizontal scaling is unsupported" in DEPLOYMENT_DOC.read_text(encoding="utf-8")


def test_rendered_deploy_compose_requires_a_password_and_binds_loopback(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker compose is not available")
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    missing = os.environ.copy()
    missing.pop("APEX_POSTGRES_PASSWORD", None)
    try:
        denied = subprocess.run(
            ["docker", "compose", "--env-file", str(empty), "-f", str(DEPLOY), "config"],
            cwd=ROOT,
            env=missing,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        pytest.skip("docker compose is not available")
    if denied.returncode != 0 and b"docker" in denied.stderr and b"not found" in denied.stderr:
        pytest.skip("docker compose is not available")
    if denied.returncode != 0 and denied.stderr.startswith(b"'docker'"):
        pytest.skip("docker compose is not available")
    assert denied.returncode != 0
    present = os.environ.copy()
    present["APEX_POSTGRES_PASSWORD"] = "compose-test-password"
    rendered = subprocess.run(
        ["docker", "compose", "--env-file", str(empty), "-f", str(DEPLOY), "config"],
        cwd=ROOT,
        env=present,
        capture_output=True,
        check=False,
    )
    if rendered.returncode != 0 and b"not found" in rendered.stderr:
        pytest.skip("docker compose is not available")
    assert rendered.returncode == 0, rendered.stderr.decode(errors="replace")[-200:]
    text = rendered.stdout.decode(errors="replace")
    assert "host_ip: 127.0.0.1" in text
    assert 'published: "8000"' in text
    assert "host_ip: 0.0.0.0" not in text
    assert 'published: "8001"' not in text
    assert 'published: "5432"' not in text
    assert "0.0.0.0:8000" not in text
    assert "8001:8001" not in text
    assert "5433:5432" not in text
    assert "apex:apex@" not in text
    assert "POSTGRES_PASSWORD: compose-test-password" in text
    assert 'APEX_DEV_AUTH_ENABLED: "false"' in text or "APEX_DEV_AUTH_ENABLED: 'false'" in text
    assert "APEX_ENVIRONMENT: production" in text
