"""Explainable ranking. Similarity is not verification."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from apex.clock import ensure_aware
from apex.persistence.models import MemoryRecord

_TOKEN = re.compile(r"[a-z0-9]+")


def rank_memory(record: MemoryRecord, query: str, now: datetime) -> tuple[int, str]:
    """Score one authorized memory. The reason lists the signals that fired."""
    score = 0
    reasons: list[str] = []
    overlap = _tokens(query) & _tokens(f"{record.content} {record.subject or ''}")
    if overlap:
        score += 10 * len(overlap)
        reasons.append("text")
    if record.reliability == "verified":
        score += 20
        reasons.append("verified")
    elif record.reliability == "observed":
        score += 10
        reasons.append("observed")
    elif record.reliability == "disputed":
        score -= 15
        reasons.append("disputed")
    elif record.reliability == "inferred":
        reasons.append("inferred")
    if now - ensure_aware(record.updated_at) <= timedelta(days=7):
        score += 5
        reasons.append("recent")
    if record.category == "constraint":
        score += 8
        reasons.append("constraint")
    if record.scope_type == "system":
        score += 4
        reasons.append("system")
    return score, ",".join(reasons) or "scope"


def _tokens(value: str) -> set[str]:
    return set(_TOKEN.findall(value.casefold()))
