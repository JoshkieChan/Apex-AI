"""Deterministic provider for tests and local development. It does not call a network."""

from __future__ import annotations

import hashlib

from apex.contracts.schemas import ModelRequest, ModelResponse, TokenUsage, VerificationStatus
from apex.interaction.media import silence_wav
from apex.providers.errors import ProviderError
from apex.providers.modalities import (
    ContentBlob,
    ProviderCapabilities,
    ProviderText,
    ProviderTurn,
)


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
        self.fail_transcribe = False
        self.fail_speech = False
        self.model_name = model_name
        self.calls: list[ModelRequest] = []
        self.turns: list[ProviderTurn] = []

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            text=True,
            image_understanding=True,
            audio_transcription=True,
            audio_generation=True,
        )

    def respond(self, turn: ProviderTurn) -> ProviderText:
        """Echo normalized parts. The text is a fixture, not evidence of perception quality."""
        self.turns.append(turn)
        if self.fail:
            raise ProviderError("unavailable", "The mock provider was configured to fail.")
        pieces = [f"turns:{len(turn.history)}"]
        for part in turn.parts:
            pieces.append(_render(part))
        return ProviderText(
            text=" ".join(pieces),
            usage=TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0),
            latency_ms=0,
        )

    def transcribe(self, audio: bytes, media_type: str, timeout: float) -> str:
        del media_type, timeout
        if self.fail_transcribe:
            raise ProviderError("timeout", "The mock transcription timed out.")
        digest = hashlib.sha256(audio).hexdigest()[:12]
        return f"transcript:{digest}"

    def synthesize(self, text: str, timeout: float) -> tuple[bytes, str]:
        del text, timeout
        if self.fail_speech:
            raise ProviderError("unavailable", "The mock speech adapter was configured to fail.")
        return silence_wav(), "audio/wav"

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


def _render(part: ContentBlob) -> str:
    if part.kind == "image":
        digest = hashlib.sha256(part.data).hexdigest()[:12]
        return f"image:{digest}"
    return part.text


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
