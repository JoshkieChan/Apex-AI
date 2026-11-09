"""Transactional memory mutations. The model does not write this table directly."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, select, union, update
from sqlalchemy.orm import Session

from apex.clock import Clock, ensure_aware
from apex.config import Settings
from apex.errors import DomainError
from apex.memory.access import assert_scope_owner, can_manage, readable, require_memory_permission
from apex.memory.contracts import (
    AUTHORITATIVE_READS,
    GOVERNING_INSTRUCTION,
    AssembledContext,
    MemoryCreate,
    MemoryForget,
    MemoryRevisionView,
    MemorySearch,
    MemoryUpdate,
    MemoryVerify,
    MemoryView,
)
from apex.memory.ranking import rank_memory
from apex.persistence.models import (
    ContextRecord,
    Delegation,
    Execution,
    Goal,
    MemoryEvent,
    MemoryPurge,
    MemoryRecord,
    MemoryRevision,
    Principal,
    Task,
    Verification,
)
from apex.persistence.repository import get_grant
from apex.redaction import redact
from apex.runtime.transitions import affected_rows
from apex.runtime_ids import new_id

_CANDIDATE_LIMIT = 200
_REDACTED = "[redacted]"
_TERMINAL_TASKS = ("completed", "failed", "cancelled")
_TERMINAL_GOALS = ("completed", "partial", "failed", "cancelled")


def _normalize_with_map(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Casefold and collapse whitespace, remembering each original source span.

    One source character can expand to several normalized characters. A run of
    whitespace becomes one space whose span covers the whole run.
    """
    stream: list[tuple[str, int, int]] = []
    for index, char in enumerate(text):
        for piece in char.casefold():
            stream.append((piece, index, index + 1))
    normalized: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(stream):
        character, start, _end = stream[cursor]
        if character.isspace():
            end = cursor
            while end < len(stream) and stream[end][0].isspace():
                end += 1
            normalized.append(" ")
            spans.append((stream[cursor][1], stream[end - 1][2]))
            cursor = end
            continue
        normalized.append(character)
        spans.append((start, stream[cursor][2]))
        cursor += 1
    return "".join(normalized), spans


def _redact_text(haystack: str, secret: str) -> tuple[str, bool]:
    """Redact a case- and whitespace-insensitive copy of a long secret.

    The length threshold applies to the normalized secret. The replacement uses
    the original source span, including characters that casefold to more than
    one character. Short secrets, hashes, suffixes, and paraphrases stay.
    """
    needle = " ".join(secret.casefold().split())
    if len(needle) < 12 or not haystack:
        return haystack, False
    normalized, spans = _normalize_with_map(haystack)
    pieces: list[str] = []
    cursor = 0
    search = 0
    changed = False
    while search <= len(normalized) - len(needle):
        found = normalized.find(needle, search)
        if found < 0:
            break
        start = spans[found][0]
        end = spans[found + len(needle) - 1][1]
        if start < cursor:
            search = found + 1
            continue
        pieces.append(haystack[cursor:start])
        pieces.append(_REDACTED)
        cursor = end
        search = found + len(needle)
        changed = True
    if not changed:
        return haystack, False
    pieces.append(haystack[cursor:])
    return "".join(pieces), True


def _redact_value(value: Any, secret: str) -> tuple[Any, bool]:
    if isinstance(value, str):
        return _redact_text(value, secret)
    if isinstance(value, dict):
        changed = False
        updated: dict[Any, Any] = {}
        for key, item in value.items():
            new_key, key_changed = _redact_value(key, secret)
            new_item, item_changed = _redact_value(item, secret)
            updated[new_key] = new_item
            changed = changed or key_changed or item_changed
        return updated, changed
    if isinstance(value, list):
        changed = False
        updated_list: list[Any] = []
        for item in value:
            new_item, item_changed = _redact_value(item, secret)
            updated_list.append(new_item)
            changed = changed or item_changed
        return updated_list, changed
    return value, False


_SUBJECT = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_META_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")
_PROJECT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class MemoryStore:
    """Create, revise, and retrieve memory for one already-authenticated principal."""

    def __init__(self, session: Session, clock: Clock, settings: Settings) -> None:
        self._session = session
        self._clock = clock
        self._settings = settings

    def create(self, actor: Principal, proposal: MemoryCreate) -> MemoryView:
        now = self._clock.now()
        scope_id = self._scope_id(actor, proposal.scope_type, proposal.scope_id)
        self._authorize_write(actor, proposal.scope_type, scope_id, now, proposal.delegation_id)
        content = _clean_content(proposal.content)
        metadata = _clean_metadata(proposal.metadata)
        reliability, lifecycle = _admission(proposal)
        if proposal.expires_at is not None and ensure_aware(proposal.expires_at) <= now:
            raise DomainError("invalid_memory", "The memory expiration is already past.")
        expires = proposal.expires_at
        if proposal.scope_type == "task" and expires is None:
            expires = now + timedelta(hours=self._settings.task_memory_ttl_hours)
        conflicts = self._conflicts(
            proposal.scope_type, scope_id, proposal.category, proposal.subject
        )
        if conflicts and _user_preference(proposal):
            for old in conflicts:
                self._supersede_row(
                    old, actor, "A newer user preference replaced this memory.", now
                )
            conflicts = []
        elif conflicts:
            reliability = "disputed"
            for old in conflicts:
                self._set_reliability(old, actor, "disputed", "conflict", now)
        record = MemoryRecord(
            id=new_id(),
            scope_type=proposal.scope_type,
            scope_id=scope_id,
            owner_principal_id=actor.id if proposal.scope_type != "system" else actor.id,
            category=proposal.category,
            content=content,
            subject=proposal.subject,
            metadata_json=metadata,
            reliability=reliability,
            lifecycle=lifecycle,
            version=1,
            supersedes_id=None,
            source_type=proposal.source_type,
            source_id=proposal.source_id,
            source_principal_id=actor.id if proposal.source_type == "user" else None,
            content_digest=_digest(content),
            verification_method=None,
            expires_at=expires,
            effective_at=now,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        self._session.add(record)
        self._session.flush()
        self._revision(record, actor, "created", now)
        self._event(record, actor.id, "created", now)
        return _view(record, now)

    def update(self, actor: Principal, memory_id: str, change: MemoryUpdate) -> MemoryView:
        now = self._clock.now()
        record = self._visible(actor, memory_id, now, change.delegation_id)
        self._authorize_write(
            actor, record.scope_type, record.scope_id, now, change.delegation_id, "memory.update"
        )
        content = _clean_content(change.content)
        reliability = "unverified" if record.reliability == "verified" else record.reliability
        self._cas(
            record,
            actor,
            content=content,
            reliability=reliability,
            lifecycle=record.lifecycle,
            verification_method=None if reliability == "unverified" else record.verification_method,
            expected_version=change.expected_version,
            reason=change.reason,
            event_type="updated",
            now=now,
        )
        return _view(record, now)

    def verify(self, actor: Principal, memory_id: str, proof: MemoryVerify) -> MemoryView:
        now = self._clock.now()
        self._expire_due(now)
        record = self._session.get(MemoryRecord, memory_id)
        if record is None or record.lifecycle == "deleted":
            raise DomainError("not_found", "The memory does not exist.")
        if proof.method != "user_confirmation":
            require_memory_permission(
                self._session,
                actor,
                "memory.verify",
                record.scope_id,
                now=now,
                delegation_id=proof.delegation_id,
            )
        try:
            readable(self._session, actor, record, now=now, delegation_id=proof.delegation_id)
        except DomainError:
            self._event(record, actor.id, "access_denied", now)
            raise
        if proof.method == "user_confirmation":
            if record.source_type != "user" or record.owner_principal_id != actor.id:
                raise DomainError("invalid_memory", "Only the source user can confirm this memory.")
            if proof.evidence_id != record.id:
                raise DomainError("invalid_memory", "The confirmation does not match this memory.")
            require_memory_permission(
                self._session,
                actor,
                "memory.read",
                record.scope_id,
                now=now,
                delegation_id=proof.delegation_id,
            )
        else:
            self._evidence(proof.method, proof.evidence_id)
        self._cas(
            record,
            actor,
            content=record.content,
            reliability="verified",
            lifecycle="active" if record.lifecycle == "candidate" else record.lifecycle,
            verification_method=proof.method,
            expected_version=proof.expected_version,
            reason=proof.method,
            event_type="verified",
            now=now,
        )
        return _view(record, now)

    def dispute(self, actor: Principal, memory_id: str, expected_version: int) -> MemoryView:
        now = self._clock.now()
        record = self._visible(actor, memory_id, now, None)
        require_memory_permission(
            self._session, actor, "memory.update", record.scope_id, now=now, delegation_id=None
        )
        self._cas(
            record,
            actor,
            content=record.content,
            reliability="disputed",
            lifecycle=record.lifecycle,
            verification_method=record.verification_method,
            expected_version=expected_version,
            reason="disputed",
            event_type="disputed",
            now=now,
        )
        return _view(record, now)

    def supersede(
        self, actor: Principal, memory_id: str, proposal: MemoryCreate, expected_version: int
    ) -> MemoryView:
        now = self._clock.now()
        current = self._visible(actor, memory_id, now, proposal.delegation_id)
        if proposal.scope_type != current.scope_type or (
            proposal.scope_id not in {None, current.scope_id}
        ):
            raise DomainError("scope_violation", "A replacement cannot move the memory scope.")
        proposal = proposal.model_copy(update={"scope_id": current.scope_id, "confirmed": True})
        self._supersede_row(current, actor, "Superseded by a new memory.", now, expected_version)
        created = self.create(actor, proposal)
        replacement = self._session.get(MemoryRecord, created.id)
        if replacement is None:
            raise DomainError("internal_error", "The replacement memory was not stored.")
        replacement.supersedes_id = current.id
        self._session.flush()
        return _view(replacement, now)

    def delete(self, actor: Principal, memory_id: str, delegation_id: str | None) -> MemoryView:
        now = self._clock.now()
        record = self._visible(actor, memory_id, now, delegation_id)
        permission = "memory.manage" if record.scope_type == "system" else "memory.delete"
        require_memory_permission(
            self._session,
            actor,
            permission,
            record.scope_id,
            now=now,
            delegation_id=delegation_id,
        )
        self._cas(
            record,
            actor,
            content=record.content,
            reliability=record.reliability,
            lifecycle="deleted",
            verification_method=record.verification_method,
            expected_version=record.version,
            reason="deleted",
            event_type="deleted",
            now=now,
            deleted_at=now,
        )
        return _view(record, now)

    def purge_due(self, actor: Principal | None = None) -> int:
        """Physically remove deleted memories past retention. The tombstone has no content."""
        now = self._clock.now()
        if actor is not None and not can_manage(self._session, actor, now):
            raise DomainError("permission_denied", "The principal cannot purge memory.")
        cutoff = now - timedelta(hours=self._settings.memory_purge_after_hours)
        rows = list(
            self._session.scalars(
                select(MemoryRecord).where(
                    MemoryRecord.lifecycle == "deleted",
                    MemoryRecord.deleted_at.is_not(None),
                    MemoryRecord.deleted_at <= cutoff,
                )
            )
        )
        for record in rows:
            revisions = list(
                self._session.scalars(
                    select(MemoryRevision).where(MemoryRevision.memory_id == record.id)
                )
            )
            self._session.execute(
                delete(MemoryRevision).where(MemoryRevision.memory_id == record.id)
            )
            self._session.execute(delete(MemoryEvent).where(MemoryEvent.memory_id == record.id))
            self._session.execute(
                update(MemoryRecord)
                .where(MemoryRecord.supersedes_id == record.id)
                .values(supersedes_id=None)
            )
            self._session.add(
                MemoryPurge(
                    id=new_id(),
                    memory_id=record.id,
                    scope_type=record.scope_type,
                    scope_id=record.scope_id,
                    revision_count=len(revisions),
                    purged_at=now,
                )
            )
            self._redact_context(record.content)
            self._session.delete(record)
        self._session.flush()
        return len(rows)

    def expire_operational_proposals(self, now: datetime | None = None) -> int:
        """Delete old untrusted proposals that are not tied to unresolved work.

        A proposal whose scope id is an unresolved task, an unresolved goal, or
        the task of an unresolved goal stays. Orphaned proposals and proposals
        for terminal work can expire. Trusted rows are not proposals.
        """
        days = self._settings.operational_retention_days
        if days <= 0:
            return 0
        moment = self._clock.now() if now is None else now
        cutoff = moment - timedelta(days=days)
        unresolved = union(
            select(Task.id.label("scope_id")).where(Task.status.notin_(_TERMINAL_TASKS)),
            select(Goal.id.label("scope_id")).where(Goal.status.notin_(_TERMINAL_GOALS)),
            select(Goal.task_id.label("scope_id")).where(Goal.status.notin_(_TERMINAL_GOALS)),
        ).subquery()
        rows = list(
            self._session.scalars(
                select(ContextRecord).where(
                    ContextRecord.trust == "proposal",
                    ContextRecord.created_at <= cutoff,
                    ContextRecord.scope_id.not_in(select(unresolved.c.scope_id)),
                )
            )
        )
        for row in rows:
            self._session.delete(row)
        return len(rows)

    def _redact_context(self, content: str) -> None:
        """Remove deleted memory text from task context rows. The row itself stays."""
        if len(" ".join(content.casefold().split())) < 12:
            return
        rows = list(self._session.scalars(select(ContextRecord)))
        for row in rows:
            redacted, changed = _redact_value(row.content, content)
            if changed and isinstance(redacted, dict):
                row.content = redacted

    def forget(self, actor: Principal, request: MemoryForget) -> int:
        now = self._clock.now()
        scope_id = self._scope_id(actor, request.scope_type, request.scope_id)
        permission = "memory.manage" if request.scope_type == "system" else "memory.delete"
        require_memory_permission(
            self._session,
            actor,
            permission,
            scope_id,
            now=now,
            delegation_id=request.delegation_id,
        )
        assert_scope_owner(self._session, actor, request.scope_type, scope_id, now=now)
        rows = list(
            self._session.scalars(
                select(MemoryRecord).where(
                    MemoryRecord.scope_type == request.scope_type,
                    MemoryRecord.scope_id == scope_id,
                    MemoryRecord.lifecycle.notin_(("deleted", "superseded")),
                )
            )
        )
        for record in rows:
            self._cas(
                record,
                actor,
                content=record.content,
                reliability=record.reliability,
                lifecycle="deleted",
                verification_method=record.verification_method,
                expected_version=record.version,
                reason="scope deleted",
                event_type="deleted",
                now=now,
                deleted_at=now,
            )
        return len(rows)

    def get(self, actor: Principal, memory_id: str, delegation_id: str | None) -> MemoryView:
        now = self._clock.now()
        self._expire_due(now)
        record = self._session.get(MemoryRecord, memory_id)
        if record is None or record.lifecycle == "deleted":
            raise DomainError("not_found", "The memory does not exist.")
        try:
            readable(self._session, actor, record, now=now, delegation_id=delegation_id)
        except DomainError:
            self._event(record, actor.id, "access_denied", now)
            raise
        return _view(record, now)

    def history(self, actor: Principal, memory_id: str) -> list[MemoryRevisionView]:
        self.get(actor, memory_id, None)
        rows = self._session.scalars(
            select(MemoryRevision)
            .where(MemoryRevision.memory_id == memory_id)
            .order_by(MemoryRevision.version)
        )
        return [
            MemoryRevisionView(
                version=row.version,
                content=row.content,
                reliability=row.reliability,
                lifecycle=row.lifecycle,
                actor_principal_id=row.actor_principal_id,
                reason=row.reason,
                created_at=row.created_at,
            )
            for row in rows
        ]

    def search(self, actor: Principal, query: MemorySearch) -> list[MemoryView]:
        now = self._clock.now()
        self._expire_due(now)
        require_memory_permission(
            self._session,
            actor,
            "memory.read",
            query.scope_id or "*",
            now=now,
            delegation_id=query.delegation_id,
        )
        rows = self._candidates(actor, query, now)
        ranked = [(rank_memory(row, query.text or "", now), row) for row in rows]
        ranked.sort(key=lambda item: (-item[0][0], item[1].id))
        views: list[MemoryView] = []
        for (score, reason), row in ranked[: query.limit]:
            del score
            view = _view(row, now)
            views.append(view.model_copy(update={"relevance": reason}))
        return views

    def assemble(
        self,
        actor: Principal,
        *,
        query: str,
        task_id: str | None,
        project_key: str | None,
        budget: int | None,
        delegation_id: str | None,
    ) -> AssembledContext:
        limit = budget if budget is not None else self._settings.memory_context_budget
        found = self.search(
            actor,
            MemorySearch(
                text=query,
                scope_id=None,
                include_candidates=False,
                limit=50,
                delegation_id=delegation_id,
            ),
        )
        selected = [
            item
            for item in found
            if _in_context(item, actor.id, task_id, project_key) and item.freshness == "current"
        ]
        required = [
            item
            for item in selected
            if item.category == "constraint" or item.scope_type == "system"
        ]
        optional = [item for item in selected if item not in required]
        included: list[MemoryView] = []
        used = 0
        exceeded = False
        for item in required:
            included.append(item)
            used += len(item.content)
        if used > limit:
            exceeded = True
        else:
            for item in optional:
                if used + len(item.content) > limit:
                    exceeded = True
                    break
                included.append(item)
                used += len(item.content)
        included_ids = [item.id for item in included]
        excluded_ids = [item.id for item in selected if item.id not in included_ids]
        return AssembledContext(
            governing_instruction=GOVERNING_INSTRUCTION,
            items=included,
            included_ids=included_ids,
            excluded_ids=excluded_ids,
            budget_characters=limit,
            used_characters=used,
            budget_exceeded=exceeded,
        )

    def record_verified_task(self, task: Task) -> None:
        """Store a verified task outcome. The caller has already passed verification."""
        existing = self._session.scalar(
            select(MemoryRecord).where(
                MemoryRecord.scope_type == "task",
                MemoryRecord.scope_id == task.id,
                MemoryRecord.category == "verified_outcome",
                MemoryRecord.lifecycle == "active",
            )
        )
        if existing is not None:
            return
        actor = self._session.get(Principal, task.principal_id)
        if actor is None:
            return
        now = self._clock.now()
        content = f"Task {task.id} completed after verification passed."
        record = MemoryRecord(
            id=new_id(),
            scope_type="task",
            scope_id=task.id,
            owner_principal_id=actor.id,
            category="verified_outcome",
            content=content,
            subject=None,
            metadata_json={},
            reliability="verified",
            lifecycle="active",
            version=1,
            supersedes_id=None,
            source_type="system",
            source_id=task.id,
            source_principal_id=None,
            content_digest=_digest(content),
            verification_method="independent_evidence",
            expires_at=now + timedelta(hours=self._settings.task_memory_ttl_hours),
            effective_at=now,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        self._session.add(record)
        self._session.flush()
        self._revision(record, actor, "verification passed", now)
        self._event(record, actor.id, "verified", now)
        self._expire_task_notes(task.id, actor, now)

    def _authorize_write(
        self,
        actor: Principal,
        scope_type: str,
        scope_id: str,
        now: datetime,
        delegation_id: str | None,
        permission: str = "memory.create",
    ) -> None:
        if scope_type == "system":
            permission = "memory.manage"
        require_memory_permission(
            self._session, actor, permission, scope_id, now=now, delegation_id=delegation_id
        )
        assert_scope_owner(self._session, actor, scope_type, scope_id, now=now)

    def _scope_id(self, actor: Principal, scope_type: str, supplied: str | None) -> str:
        if scope_type == "personal":
            if supplied not in {None, actor.id}:
                raise DomainError(
                    "permission_denied", "The principal cannot use that memory scope."
                )
            return actor.id
        if scope_type == "system":
            if supplied not in {None, "system"}:
                raise DomainError("invalid_memory", "System memory uses the system scope.")
            return "system"
        if supplied is None:
            raise DomainError("invalid_memory", "The memory scope is missing.")
        if scope_type == "project" and _PROJECT.fullmatch(supplied) is None:
            raise DomainError("invalid_memory", "The project scope is not valid.")
        if scope_type == "task" and _SUBJECT.fullmatch(supplied) is None:
            raise DomainError("invalid_memory", "The task scope is not valid.")
        return supplied

    def _visible(
        self, actor: Principal, memory_id: str, now: datetime, delegation_id: str | None
    ) -> MemoryRecord:
        self._expire_due(now)
        record = self._session.get(MemoryRecord, memory_id)
        if record is None or record.lifecycle == "deleted":
            raise DomainError("not_found", "The memory does not exist.")
        try:
            readable(self._session, actor, record, now=now, delegation_id=delegation_id)
        except DomainError:
            self._event(record, actor.id, "access_denied", now)
            raise
        return record

    def _conflicts(
        self, scope_type: str, scope_id: str, category: str, subject: str | None
    ) -> list[MemoryRecord]:
        if not subject:
            return []
        rows = self._session.scalars(
            select(MemoryRecord).where(
                MemoryRecord.scope_type == scope_type,
                MemoryRecord.scope_id == scope_id,
                MemoryRecord.category == category,
                MemoryRecord.subject == subject,
                MemoryRecord.lifecycle == "active",
            )
        )
        return list(rows)

    def _evidence(self, method: str, evidence_id: str) -> None:
        if method == "authoritative_read":
            execution = self._session.get(Execution, evidence_id)
            verification = (
                None
                if execution is None
                else self._session.scalar(
                    select(Verification).where(Verification.execution_id == execution.id)
                )
            )
            if (
                execution is None
                or execution.status != "succeeded"
                or execution.capability_id not in AUTHORITATIVE_READS
                or verification is None
                or verification.status != "passed"
            ):
                raise DomainError(
                    "invalid_memory",
                    "Authoritative verification needs a passed read of independent state.",
                )
            return
        verification = self._session.get(Verification, evidence_id)
        if verification is None or verification.status != "passed":
            raise DomainError(
                "invalid_memory",
                "Independent verification needs a passed verification record.",
            )

    def _candidates(
        self, actor: Principal, query: MemorySearch, now: datetime
    ) -> list[MemoryRecord]:
        lifecycles = ["active"]
        if query.include_candidates:
            lifecycles.append("candidate")
        statement = select(MemoryRecord).where(MemoryRecord.lifecycle.in_(lifecycles))
        statement = statement.where(
            or_(MemoryRecord.expires_at.is_(None), MemoryRecord.expires_at > now)
        )
        if query.scope_type is not None:
            statement = statement.where(MemoryRecord.scope_type == query.scope_type)
        if query.scope_id is not None:
            statement = statement.where(MemoryRecord.scope_id == query.scope_id)
        if query.category is not None:
            statement = statement.where(MemoryRecord.category == query.category)
        if query.reliability is not None:
            statement = statement.where(MemoryRecord.reliability == query.reliability)
        if not can_manage(self._session, actor, now):
            statement = statement.where(self._owner_clause(actor, query.delegation_id))
        elif query.scope_type != "personal":
            statement = statement.where(
                or_(
                    MemoryRecord.scope_type != "personal",
                    MemoryRecord.owner_principal_id == actor.id,
                    MemoryRecord.scope_id == query.scope_id,
                )
            )
        rows = list(self._session.scalars(statement.limit(_CANDIDATE_LIMIT)))
        return [
            row
            for row in rows
            if row.lifecycle != "deleted" and _allowed_scope(self, actor, row, now, query)
        ]

    def _owner_clause(self, actor: Principal, delegation_id: str | None) -> Any:
        task_ids = select(Task.id).where(Task.principal_id == actor.id)
        if delegation_id:
            delegation = self._session.get(Delegation, delegation_id)
            scope = delegation.resource_scope if delegation is not None else ""
        else:
            grant = get_grant(self._session, actor.id, "memory.read")
            scope = grant.resource_scope if grant is not None else ""
        project = MemoryRecord.scope_type == "project"
        if scope == "*":
            project_clause = project
        else:
            allowed = [part.strip() for part in scope.split(",") if part.strip()]
            project_clause = project & MemoryRecord.scope_id.in_(allowed or ["__none__"])
        return or_(
            (MemoryRecord.scope_type == "personal") & (MemoryRecord.owner_principal_id == actor.id),
            MemoryRecord.scope_type == "system",
            project_clause,
            (MemoryRecord.scope_type == "task") & (MemoryRecord.scope_id.in_(task_ids)),
        )

    def _cas(
        self,
        record: MemoryRecord,
        actor: Principal,
        *,
        content: str,
        reliability: str,
        lifecycle: str,
        verification_method: str | None,
        expected_version: int,
        reason: str,
        event_type: str,
        now: datetime,
        deleted_at: datetime | None = None,
    ) -> None:
        result = self._session.execute(
            update(MemoryRecord)
            .where(MemoryRecord.id == record.id, MemoryRecord.version == expected_version)
            .values(
                content=content,
                content_digest=_digest(content),
                reliability=reliability,
                lifecycle=lifecycle,
                verification_method=verification_method,
                version=expected_version + 1,
                updated_at=now,
                deleted_at=deleted_at if lifecycle == "deleted" else record.deleted_at,
            )
        )
        if affected_rows(result) != 1:
            raise DomainError("conflict", "The memory changed and the write was rejected.")
        self._session.refresh(record)
        self._revision(record, actor, reason, now)
        self._event(record, actor.id, event_type, now)

    def _set_reliability(
        self, record: MemoryRecord, actor: Principal, reliability: str, reason: str, now: datetime
    ) -> None:
        self._cas(
            record,
            actor,
            content=record.content,
            reliability=reliability,
            lifecycle=record.lifecycle,
            verification_method=record.verification_method,
            expected_version=record.version,
            reason=reason,
            event_type="disputed",
            now=now,
        )

    def _supersede_row(
        self,
        record: MemoryRecord,
        actor: Principal,
        reason: str,
        now: datetime,
        expected_version: int | None = None,
    ) -> None:
        self._cas(
            record,
            actor,
            content=record.content,
            reliability="superseded",
            lifecycle="superseded",
            verification_method=record.verification_method,
            expected_version=record.version if expected_version is None else expected_version,
            reason=reason,
            event_type="superseded",
            now=now,
        )

    def _expire_due(self, now: datetime) -> None:
        rows = list(
            self._session.scalars(
                select(MemoryRecord)
                .where(
                    MemoryRecord.lifecycle == "active",
                    MemoryRecord.expires_at.is_not(None),
                    MemoryRecord.expires_at <= now,
                )
                .limit(100)
            )
        )
        for record in rows:
            actor = self._session.get(Principal, record.owner_principal_id)
            if actor is None:
                continue
            self._cas(
                record,
                actor,
                content=record.content,
                reliability=record.reliability,
                lifecycle="expired",
                verification_method=record.verification_method,
                expected_version=record.version,
                reason="expired",
                event_type="expired",
                now=now,
            )

    def _expire_task_notes(self, task_id: str, actor: Principal, now: datetime) -> None:
        rows = list(
            self._session.scalars(
                select(MemoryRecord).where(
                    MemoryRecord.scope_type == "task",
                    MemoryRecord.scope_id == task_id,
                    MemoryRecord.category != "verified_outcome",
                    MemoryRecord.lifecycle.in_(("active", "candidate")),
                )
            )
        )
        for record in rows:
            self._cas(
                record,
                actor,
                content=record.content,
                reliability=record.reliability,
                lifecycle="expired",
                verification_method=record.verification_method,
                expected_version=record.version,
                reason="task completed",
                event_type="expired",
                now=now,
            )

    def _revision(self, record: MemoryRecord, actor: Principal, reason: str, now: datetime) -> None:
        self._session.add(
            MemoryRevision(
                id=new_id(),
                memory_id=record.id,
                version=record.version,
                content=record.content,
                reliability=record.reliability,
                lifecycle=record.lifecycle,
                actor_principal_id=actor.id,
                reason=reason[:500],
                created_at=now,
            )
        )
        self._session.flush()

    def _event(
        self, record: MemoryRecord, actor_id: str | None, event_type: str, now: datetime
    ) -> None:
        self._session.add(
            MemoryEvent(
                id=new_id(),
                memory_id=record.id,
                event_type=event_type,
                actor_principal_id=actor_id,
                detail={
                    "version": record.version,
                    "reliability": record.reliability,
                    "lifecycle": record.lifecycle,
                    "scope_type": record.scope_type,
                },
                created_at=now,
            )
        )
        self._session.flush()


def _allowed_scope(
    store: MemoryStore, actor: Principal, row: MemoryRecord, now: datetime, query: MemorySearch
) -> bool:
    try:
        readable(store._session, actor, row, now=now, delegation_id=query.delegation_id)
    except DomainError:
        return False
    return True


def _admission(proposal: MemoryCreate) -> tuple[str, str]:
    if proposal.source_type == "model":
        return "inferred", "candidate"
    if proposal.source_type == "user" and proposal.confirmed:
        return "observed", "active"
    if proposal.source_type == "tool" and proposal.confirmed:
        return "observed", "active"
    if proposal.source_type == "task" and proposal.confirmed:
        return "observed", "active"
    return "unverified", "candidate"


def _user_preference(proposal: MemoryCreate) -> bool:
    return (
        proposal.category == "preference" and proposal.source_type == "user" and proposal.confirmed
    )


def _clean_content(content: str) -> str:
    text = content.strip()
    if not text or redact(text) != text or "PRIVATE KEY" in text:
        raise DomainError("sensitive_content", "The memory content cannot be stored.")
    return text


def _clean_metadata(metadata: dict[str, str]) -> dict[str, str]:
    if len(metadata) > 8:
        raise DomainError("invalid_memory", "The memory metadata is too large.")
    clean: dict[str, str] = {}
    for key, value in metadata.items():
        if _META_KEY.fullmatch(key) is None or not isinstance(value, str):
            raise DomainError("invalid_memory", "The memory metadata is not valid.")
        if len(value) > 200 or redact(value) != value:
            raise DomainError("sensitive_content", "The memory content cannot be stored.")
        clean[key] = value
    return clean


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _view(record: MemoryRecord, now: datetime) -> MemoryView:
    stale = record.lifecycle in {"expired", "superseded", "deleted"} or (
        record.expires_at is not None and ensure_aware(record.expires_at) <= now
    )
    return MemoryView(
        id=record.id,
        scope_type=record.scope_type,  # type: ignore[arg-type]
        scope_id=record.scope_id,
        owner_principal_id=record.owner_principal_id,
        category=record.category,  # type: ignore[arg-type]
        content=record.content,
        subject=record.subject,
        reliability=record.reliability,  # type: ignore[arg-type]
        lifecycle=record.lifecycle,  # type: ignore[arg-type]
        version=record.version,
        supersedes_id=record.supersedes_id,
        source_type=record.source_type,  # type: ignore[arg-type]
        source_id=record.source_id,
        content_digest=record.content_digest,
        verification_method=record.verification_method,
        freshness="stale" if stale else "current",
        expires_at=record.expires_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _in_context(
    item: MemoryView, actor_id: str, task_id: str | None, project_key: str | None
) -> bool:
    if item.lifecycle not in {"active"}:
        return False
    if item.reliability == "superseded":
        return False
    if item.scope_type == "personal":
        return item.owner_principal_id == actor_id
    if item.scope_type == "task":
        return task_id is not None and item.scope_id == task_id
    if item.scope_type == "project":
        return project_key is not None and item.scope_id == project_key
    return item.scope_type == "system"
