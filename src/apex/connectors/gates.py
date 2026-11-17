"""Execution-time connection ceilings. A missing gate does not grant access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from apex.clock import ensure_aware
from apex.errors import DomainError


@dataclass(frozen=True)
class ConnectionGate:
    """The live ceiling for one connection or MCP server."""

    state: str
    scopes: frozenset[str]
    project_key: str | None
    expires_at: datetime | None


_GATES: dict[str, ConnectionGate] = {}


def set_gate(key: str, gate: ConnectionGate | None) -> None:
    """Install or remove the ceiling checked before an adapter runs."""
    if gate is None:
        _GATES.pop(key, None)
        return
    _GATES[key] = gate


def enforce(
    key: str,
    permission: str,
    project_key: str | None,
    now: datetime,
) -> None:
    """Reject work when a ceiling exists and does not allow this call.

    Legacy MCP servers have no ceiling here. Their registry and grant checks
    still apply. A present ceiling that is not active blocks the call before
    the external operation is sent.
    """
    gate = _GATES.get(key)
    if gate is None:
        return
    if gate.state != "active":
        raise DomainError(
            "connection_unavailable",
            "The connection cannot accept work.",
            dispatched=False,
        )
    if gate.expires_at is not None and ensure_aware(gate.expires_at) <= ensure_aware(now):
        raise DomainError(
            "credential_expired",
            "The connection authorization has expired.",
            dispatched=False,
        )
    if permission not in gate.scopes:
        raise DomainError(
            "insufficient_scope",
            "The connection scope does not allow this operation.",
            dispatched=False,
        )
    if gate.project_key is not None and project_key != gate.project_key:
        raise DomainError(
            "project_mismatch",
            "The connection belongs to another project.",
            dispatched=False,
        )


def reset_gates() -> None:
    """Drop process-local ceilings. Tests call this between cases."""
    _GATES.clear()
