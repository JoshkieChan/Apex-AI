"""In-process capability registry. Registration is explicit."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from pydantic import BaseModel

from apex.capabilities.schema_hash import schema_digest
from apex.contracts.schemas import RiskLevel

Adapter = Callable[[Mapping[str, object], float], dict[str, object]]


@dataclass(frozen=True)
class CapabilitySpec:
    """Description of one executable capability and the adapter that implements it."""

    id: str
    name: str
    description: str
    permission_key: str
    risk: RiskLevel
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    adapter: Adapter
    verification_kind: str
    available: bool = True
    timeout_seconds: float = 2.0
    max_attempts: int = 1
    max_input_bytes: int = 64_000
    version: str = "1"
    schema_hash: str = ""
    idempotent: bool = True
    resource_fields: tuple[str, ...] = ()
    supports_status_reconciliation: bool = False
    supports_independent_readback: bool = False
    supports_cancellation: bool = False
    verification_method: str = "recompute"


class CapabilityRegistry:
    """The only lookup path from a capability id to an adapter."""

    def __init__(self) -> None:
        self._items: dict[str, CapabilitySpec] = {}

    def register(self, spec: CapabilitySpec) -> None:
        if spec.id in self._items:
            raise ValueError(f"capability already registered: {spec.id}")
        if not spec.schema_hash:
            spec = replace(spec, schema_hash=schema_digest(spec.input_model))
        self._items[spec.id] = spec

    def replace(self, spec: CapabilitySpec) -> None:
        """Install a new version of a capability. Callers must bump the version first."""
        if not spec.schema_hash:
            spec = replace(spec, schema_hash=schema_digest(spec.input_model))
        self._items[spec.id] = spec

    def disable(self, capability_id: str) -> None:
        current = self._items.get(capability_id)
        if current is None:
            return
        self._items[capability_id] = replace(current, available=False)

    def get(self, capability_id: str) -> CapabilitySpec | None:
        return self._items.get(capability_id)

    def list(self) -> list[CapabilitySpec]:
        return [self._items[key] for key in sorted(self._items)]
