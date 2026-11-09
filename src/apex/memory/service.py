"""Persist trusted task outcomes separately from untrusted proposals."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from apex.clock import Clock
from apex.config import Settings
from apex.contracts.schemas import ContextScope, TrustLevel
from apex.errors import DomainError
from apex.memory.contracts import AssembledContext, MemoryView
from apex.memory.store import MemoryStore
from apex.persistence.models import ContextRecord, Principal, Task
from apex.runtime_ids import new_id


class MemoryService:
    """Write scoped context. Nothing in this service grants authority."""

    def __init__(self, session: Session, clock: Clock, settings: Settings) -> None:
        self._session = session
        self._clock = clock
        self._store = MemoryStore(session, clock, settings)

    def remember_verified_outcome(self, task: Task, summary: dict[str, Any]) -> ContextRecord:
        """Store a trusted task-scoped record after verification has passed."""
        record = ContextRecord(
            id=new_id(),
            scope_type=ContextScope.task.value,
            scope_id=task.id,
            principal_id=task.principal_id,
            trust=TrustLevel.trusted.value,
            kind="verified_outcome",
            content=summary,
            created_at=self._clock.now(),
        )
        self._session.add(record)
        self._session.flush()
        return record

    def propose(
        self,
        *,
        scope: ContextScope,
        scope_id: str,
        principal_id: str | None,
        kind: str,
        content: dict[str, Any],
    ) -> ContextRecord:
        """Store an untrusted proposal. Callers must not treat it as authority or fact."""
        record = ContextRecord(
            id=new_id(),
            scope_type=scope.value,
            scope_id=scope_id,
            principal_id=principal_id,
            trust=TrustLevel.proposal.value,
            kind=kind,
            content=content,
            created_at=self._clock.now(),
        )
        self._session.add(record)
        self._session.flush()
        return record

    def record_verified_task(self, task: Task) -> None:
        """Remember that verification passed, without copying tool output into memory."""
        self._store.record_verified_task(task)

    def assemble_for_task(self, task: Task) -> AssembledContext | None:
        actor = self._session.get(Principal, task.principal_id)
        if actor is None:
            return None
        try:
            return self._store.assemble(
                actor,
                query=task.request_text,
                task_id=task.id,
                project_key=task.project_key,
                budget=None,
                delegation_id=task.delegation_id,
            )
        except DomainError:
            return None

    @property
    def store(self) -> MemoryStore:
        return self._store


def context_for_provider(
    context: AssembledContext | None,
) -> tuple[list[tuple[str, str]], dict[str, Any] | None]:
    """Return provider messages and a structured copy. Empty context adds nothing."""
    if context is None or not context.items:
        return [], None
    payload = {
        "kind": "MEMORY_DATA",
        "governing_instruction": context.governing_instruction,
        "items": [item.model_dump(mode="json") for item in context.items],
        "included_ids": context.included_ids,
        "excluded_ids": context.excluded_ids,
    }
    messages = [
        ("system", context.governing_instruction),
        ("user", "MEMORY_DATA " + json.dumps(payload, sort_keys=True)),
    ]
    return messages, payload


# Re-export for callers that only need the view type.
__all__ = ["MemoryService", "MemoryView", "context_for_provider"]
