"""Connections, connector operations, and devices.

Revision ID: 0008_connectors
Revises: 0007_interaction
Create Date: 2026-11-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_connectors"
down_revision: str | None = "0007_interaction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add connection records. Secret values are not columns."""
    op.create_table(
        "connections",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("connection_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("owner_principal_id", sa.String(length=36), nullable=False),
        sa.Column("project_key", sa.String(length=64), nullable=True),
        sa.Column("external_identity", sa.String(length=256), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=True),
        sa.Column("auth_method", sa.String(length=32), nullable=False),
        sa.Column("secret_ref", sa.String(length=256), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("trust", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("mcp_server_id", sa.String(length=36), nullable=True),
        sa.Column("capability_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('mcp', 'api', 'device')", name="ck_connections_kind"),
        sa.CheckConstraint(
            "state IN ("
            "'pending_configuration', 'pending_authorization', 'active', "
            "'degraded', 'disabled', 'revoked', 'failed')",
            name="ck_connections_state",
        ),
        sa.CheckConstraint("trust IN ('untrusted', 'reviewed')", name="ck_connections_trust"),
        sa.ForeignKeyConstraint(
            ["mcp_server_id"],
            ["mcp_servers.id"],
            name="fk_connections_mcp_server",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_principal_id"],
            ["principals.id"],
            name="fk_connections_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_key", name="uq_connections_connection_key"),
    )
    op.create_table(
        "connection_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=False),
        sa.Column("operation_key", sa.String(length=64), nullable=False),
        sa.Column("capability_id", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("output_schema", sa.JSON(), nullable=False),
        sa.Column("schema_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("risk", sa.String(length=32), nullable=True),
        sa.Column("permission_key", sa.String(length=128), nullable=True),
        sa.Column("side_effect", sa.String(length=32), nullable=False),
        sa.Column("registration_state", sa.String(length=32), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "registration_state IN ("
            "'discovered', 'registered', 'disabled', 'revoked', 'schema_changed')",
            name="ck_connection_operations_state",
        ),
        sa.CheckConstraint(
            "side_effect IN ('read', 'idempotent_write', 'non_idempotent_write')",
            name="ck_connection_operations_side_effect",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name="fk_connection_operations_connection",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("capability_id", name="uq_connection_operations_capability"),
        sa.UniqueConstraint(
            "connection_id",
            "operation_key",
            name="uq_connection_operations_connection_operation",
        ),
    )
    op.create_table(
        "devices",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("device_key", sa.String(length=64), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=False),
        sa.Column("device_type", sa.String(length=64), nullable=False),
        sa.Column("owner_principal_id", sa.String(length=36), nullable=False),
        sa.Column("pairing_state", sa.String(length=32), nullable=False),
        sa.Column("pairing_code_hash", sa.String(length=64), nullable=False),
        sa.Column("protocol_version", sa.String(length=32), nullable=False),
        sa.Column("health", sa.String(length=32), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "pairing_state IN ('unpaired', 'paired', 'revoked')",
            name="ck_devices_pairing_state",
        ),
        sa.CheckConstraint(
            "health IN ('unknown', 'ok', 'unavailable')",
            name="ck_devices_health",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name="fk_devices_connection",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_principal_id"],
            ["principals.id"],
            name="fk_devices_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", name="uq_devices_connection"),
        sa.UniqueConstraint("device_key", name="uq_devices_device_key"),
    )


def downgrade() -> None:
    """Drop connector tables. Audit rows in other tables stay."""
    op.drop_table("devices")
    op.drop_table("connection_operations")
    op.drop_table("connections")
