"""Process configuration. Secrets come from the environment and are never hardcoded."""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings.

    Development authentication is off unless explicitly enabled, and the application
    refuses to start it when the environment is production.
    """

    model_config = SettingsConfigDict(
        env_prefix="APEX_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite+pysqlite:///:memory:"
    dev_auth_enabled: bool = False
    seed_dev_principals: bool = False
    log_level: str = "INFO"
    model_provider: Literal["mock", "openai", "anthropic"] = "mock"
    instance_id: str = "local"
    lease_seconds: int = 60
    approval_ttl_hours: int = 24
    max_task_attempts: int = 3
    request_timeout_seconds: float = 30.0
    worker_interval_seconds: float = 5.0

    openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("OPENAI_API_KEY", "APEX_OPENAI_API_KEY"),
    )
    openai_model: str = Field(
        default="",
        validation_alias=AliasChoices("OPENAI_MODEL", "APEX_OPENAI_MODEL"),
    )
    openai_base_url: str = Field(
        default="https://api.openai.com/v1",
        validation_alias=AliasChoices("OPENAI_BASE_URL", "APEX_OPENAI_BASE_URL"),
    )
    anthropic_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "APEX_ANTHROPIC_API_KEY"),
    )
    anthropic_model: str = Field(
        default="",
        validation_alias=AliasChoices("ANTHROPIC_MODEL", "APEX_ANTHROPIC_MODEL"),
    )
    anthropic_base_url: str = Field(
        default="https://api.anthropic.com",
        validation_alias=AliasChoices("ANTHROPIC_BASE_URL", "APEX_ANTHROPIC_BASE_URL"),
    )
    anthropic_version: str = Field(
        default="2026-10-21",
        validation_alias=AliasChoices("ANTHROPIC_VERSION", "APEX_ANTHROPIC_VERSION"),
    )
    auth_issuer: str = ""
    auth_audience: str = ""
    auth_public_key_pem: str = ""
    auth_private_key_pem: str = ""
    auth_jwks_url: str = ""
    auth_jwks_refresh_seconds: int = 30
    auth_jwks_cache_seconds: int = 300
    auth_jwks_timeout_seconds: float = 2.0
    auth_jwks_max_bytes: int = 65_536
    auth_jwks_allow_http_fixture: bool = False
    dev_token_issuance: bool = False
    dev_token_bootstrap: str = ""
    mcp_allow_local_fixture: bool = False
    mcp_timeout_seconds: float = 5.0
    mcp_max_bytes: int = 1_000_000
    memory_context_budget: int = 2000
    task_memory_ttl_hours: int = 168
    secret_provider: Literal["env", "file", "vault"] = "env"  # noqa: S105
    secret_dir: str = ""
    vault_addr: str = ""
    vault_token_env: str = "APEX_VAULT_TOKEN"  # noqa: S105
    vault_mount: str = "secret"
    secret_cache_seconds: int = 30
    require_external_secrets: bool = False
    memory_purge_after_hours: int = Field(default=24, ge=0, le=24 * 365)
    backup_retention_days: int = Field(default=30, ge=1, le=3650)
    max_goal_subtasks: int = Field(default=8, ge=1, le=32)
    max_plan_revisions: int = Field(default=3, ge=1, le=8)
    max_agent_invocations: int = Field(default=12, ge=1, le=32)
    max_goal_concurrency: int = Field(default=2, ge=1, le=4)
    max_goal_recoveries: int = Field(default=3, ge=1, le=8)
    max_goals_per_recovery: int = Field(default=4, ge=1, le=16)
    auth_oidc_discovery: bool = False


def load_settings() -> Settings:
    """Load settings from the environment and an optional .env file."""
    return Settings()


def assert_runtime_is_allowed(settings: Settings) -> None:
    """Refuse combinations that would present development controls as production auth."""
    if settings.environment == "production" and settings.dev_auth_enabled:
        raise RuntimeError(
            "Development authentication cannot be enabled when APEX_ENVIRONMENT=production."
        )
    if settings.environment == "production" and settings.seed_dev_principals:
        raise RuntimeError(
            "Development principal seeding cannot run when APEX_ENVIRONMENT=production."
        )
    if settings.environment == "production" and settings.auth_private_key_pem:
        raise RuntimeError("Production must not hold the development signing key.")
    if settings.environment == "production" and (
        settings.dev_token_issuance or settings.dev_token_bootstrap
    ):
        raise RuntimeError(
            "Development token issuance cannot be enabled when APEX_ENVIRONMENT=production."
        )
    if settings.environment == "production" and (
        settings.auth_jwks_allow_http_fixture or settings.mcp_allow_local_fixture
    ):
        raise RuntimeError(
            "Local network fixture exceptions cannot be enabled when APEX_ENVIRONMENT=production."
        )
    if settings.environment == "production" and settings.auth_jwks_url.startswith("http://"):
        raise RuntimeError("Production JWKS must use HTTPS.")
    if settings.environment == "production" and (
        settings.auth_oidc_discovery and settings.auth_issuer.startswith("http://")
    ):
        raise RuntimeError("Production OIDC discovery must use HTTPS.")
    if settings.environment == "production" and settings.require_external_secrets:
        if settings.secret_provider == "env":  # noqa: S105
            raise RuntimeError(
                "Production requires an external secret provider, "
                "and the env provider cannot supply it."
            )
        if settings.secret_provider == "file" and not settings.secret_dir:  # noqa: S105
            raise RuntimeError("Production file secret provider requires APEX_SECRET_DIR.")
        if settings.secret_provider == "vault" and not settings.vault_addr:  # noqa: S105
            raise RuntimeError("Production Vault provider requires APEX_VAULT_ADDR.")
    identity_ready = bool(
        settings.auth_issuer
        and settings.auth_audience
        and (
            settings.auth_public_key_pem
            or settings.auth_jwks_url
            or (settings.auth_oidc_discovery and settings.auth_issuer.startswith("https://"))
        )
    )
    any_piece = any(
        (
            settings.auth_issuer,
            settings.auth_audience,
            settings.auth_public_key_pem,
            settings.auth_jwks_url,
        )
    )
    if settings.environment == "production" and any_piece and not identity_ready:
        raise RuntimeError("Production authentication configuration is incomplete.")
