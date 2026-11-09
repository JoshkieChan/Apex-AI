"""Identifier and canonical hashing helpers."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


def new_id() -> str:
    """Return a random UUID string."""
    return str(uuid.uuid4())


def canonical_json(value: Any) -> str:
    """Serialize JSON with stable key order and no insignificant whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_operation(
    capability_id: str,
    payload: dict[str, Any],
    bindings: dict[str, Any],
    upstream_hashes: list[str],
    *,
    capability_version: str = "1",
    schema_hash: str = "",
    principal_id: str = "",
    delegation_id: str = "",
    resource_scope: str = "",
) -> str:
    """Bind an operation to its version, principal, scope, payload, and upstream steps."""
    body = {
        "capability_id": capability_id,
        "capability_version": capability_version,
        "schema_hash": schema_hash,
        "principal_id": principal_id,
        "delegation_id": delegation_id,
        "resource_scope": resource_scope,
        "input": payload,
        "bindings": bindings,
        "upstream": upstream_hashes,
    }
    digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    return digest


def export_receipt(content: dict[str, Any], destination_label: str) -> dict[str, Any]:
    """Deterministic simulated export receipt. This function does not perform I/O."""
    encoded = canonical_json(content).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    return {
        "receipt_id": f"exp_{digest}",
        "byte_count": len(encoded),
        "destination_label": destination_label,
    }
