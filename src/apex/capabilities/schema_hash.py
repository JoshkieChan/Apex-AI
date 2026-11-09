"""Stable schema identity for approval binding."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel

from apex.runtime_ids import canonical_json


def schema_digest(model: type[BaseModel]) -> str:
    """Hash the JSON schema so a changed contract does not keep an old approval."""
    encoded = canonical_json(model.model_json_schema()).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
