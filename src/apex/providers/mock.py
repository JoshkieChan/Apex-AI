"""Deterministic provider for tests and local development. It does not call a network."""

from __future__ import annotations

from apex.contracts.schemas import ModelRequest, ModelResponse, TokenUsage, VerificationStatus
from apex.providers.errors import ProviderError


class MockModelProvider:
    """Return scripted JSON for a schema name, otherwise a conservative default."""

    name = "mock"

    def __init__(
        self,
        scripts: dict[str, dict[str, object]] | None = None,
        *,
        fail: bool = False,
        model_name: str = "mock-deterministic",
    ) -> None:
        self.scripts = scripts or {}
        self.fail = fail
        self.model_name = model_name
        self.calls: list[ModelRequest] = []

    def complete_structured(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        if self.fail:
            raise ProviderError("unavailable", "The mock provider was configured to fail.")
        content = self.scripts.get(request.schema_name, _default_for(request.schema_name))
        return ModelResponse(
            provider=self.name,
            model=self.model_name,
            content=content,
            usage=TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0),
            latency_ms=0,
        )


def _default_for(schema_name: str) -> dict[str, object]:
    if schema_name == "CoordinationNote":
        return {
            "summary": "Use the deterministic planner. Suggestions are not authorization.",
            "suggested_capabilities": ["text.analyze"],
        }
    if schema_name == "IntentDraft":
        return {
            "objective": "Advisory interpretation only.",
            "required_capabilities": ["text.analyze"],
            "constraints": [],
            "risk": "unknown",
            "expected_outcome": "The deterministic classifier remains authoritative.",
            "missing_information": [],
            "clarification_required": False,
            "sensitivity_flags": [],
        }
    if schema_name == "VerificationNote":
        return {
            "note": "Advisory semantic review only.",
            "suggested_status": VerificationStatus.passed.value,
        }
    return {}
