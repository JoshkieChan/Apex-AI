"""Local demonstration capabilities. They do not touch the host, network, or filesystem."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from apex.capabilities.registry import CapabilityRegistry, CapabilitySpec
from apex.contracts.schemas import RiskLevel
from apex.runtime_ids import export_receipt


class TextAnalyzeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=10_000)


class TextAnalyzeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    word_count: int = Field(ge=0)
    character_count: int = Field(ge=0)
    preview: str


class ExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination_label: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    content: dict[str, Any]


class ExportOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt_id: str
    byte_count: int = Field(ge=0)
    destination_label: str


def analyze_text(payload: Mapping[str, object], timeout_seconds: float) -> dict[str, object]:
    """Count words and characters. `timeout_seconds` is accepted and not used for I/O."""
    del timeout_seconds
    text = str(payload["text"])
    return {
        "word_count": len(text.split()),
        "character_count": len(text),
        "preview": text[:80],
    }


def export_record(payload: Mapping[str, object], timeout_seconds: float) -> dict[str, object]:
    """Return a deterministic receipt. Nothing is sent anywhere."""
    del timeout_seconds
    content = payload["content"]
    if not isinstance(content, dict):
        raise ValueError("export content must be an object")
    label = str(payload["destination_label"])
    return export_receipt(content, label)


def register_builtins(registry: CapabilityRegistry) -> None:
    """Register the demonstration capabilities."""
    registry.register(
        CapabilitySpec(
            id="text.analyze",
            name="Analyze text",
            description="Count the words and characters in a text value.",
            permission_key="text.analyze",
            risk=RiskLevel.low,
            input_model=TextAnalyzeInput,
            output_model=TextAnalyzeOutput,
            adapter=analyze_text,
            verification_kind="text.analyze",
            timeout_seconds=2.0,
            max_attempts=2,
            idempotent=True,
            verification_method="recompute",
        )
    )
    registry.register(
        CapabilitySpec(
            id="records.export",
            name="Export record",
            description="Simulate an export receipt for a JSON object and a destination label.",
            permission_key="records.export",
            risk=RiskLevel.high,
            input_model=ExportInput,
            output_model=ExportOutput,
            adapter=export_record,
            verification_kind="records.export",
            resource_fields=("destination_label",),
            timeout_seconds=2.0,
            max_attempts=1,
            idempotent=True,
            verification_method="recompute",
        )
    )


def build_default_registry() -> CapabilityRegistry:
    """Return a registry containing only the built-in demonstration capabilities."""
    registry = CapabilityRegistry()
    register_builtins(registry)
    return registry
