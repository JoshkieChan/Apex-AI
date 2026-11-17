"""Trusted project scope for one execution. Model output cannot set it."""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar, Token

from apex.errors import DomainError

_project: ContextVar[str | None] = ContextVar("apex_execution_project", default=None)


def bind_project(project_key: str | None) -> Token[str | None]:
    """Install the task project for adapters on this call."""
    return _project.set(project_key)


def reset_project(token: Token[str | None]) -> None:
    """Restore the previous project scope."""
    _project.reset(token)


def current_project() -> str | None:
    """Return the project bound by the execution engine."""
    return _project.get()


def execution_project(payload: Mapping[str, object]) -> str | None:
    """Return the project for a connection check.

    The task project is authoritative. A payload project may name the same
    project. A different payload project is rejected. A missing project stays
    missing so a scoped connection fails closed.
    """
    bound = current_project()
    raw = payload.get("project_key")
    supplied = raw if isinstance(raw, str) else None
    if bound is None:
        return None
    if supplied is not None and bound != supplied:
        raise DomainError(
            "project_mismatch",
            "The connection belongs to another project.",
            dispatched=False,
        )
    return bound
