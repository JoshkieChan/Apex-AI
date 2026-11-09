"""Wire a request-scoped orchestrator. Engines are not process-wide singletons."""

from __future__ import annotations

from sqlalchemy.orm import Session

from apex.capabilities.registry import CapabilityRegistry
from apex.clock import Clock
from apex.config import Settings
from apex.providers.base import ModelProvider
from apex.runtime.orchestrator import Orchestrator, RuntimeContext


def build_orchestrator(
    *,
    session: Session,
    settings: Settings,
    registry: CapabilityRegistry,
    provider: ModelProvider | None,
    clock: Clock,
) -> Orchestrator:
    """Build an orchestrator bound to the caller's session."""
    return Orchestrator(
        RuntimeContext(
            session=session,
            registry=registry,
            provider=provider,
            settings=settings,
            clock=clock,
        )
    )
