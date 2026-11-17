"""Provider interface. Adapters normalize errors and return schema-shaped dictionaries."""

from __future__ import annotations

from typing import Protocol

from apex.contracts.schemas import ModelRequest, ModelResponse
from apex.providers.modalities import ProviderCapabilities, ProviderText, ProviderTurn


class ModelProvider(Protocol):
    """Structured completion and explicit multimodal operations."""

    name: str
    model_name: str

    def complete_structured(self, request: ModelRequest) -> ModelResponse:
        """Invoke the provider and return parsed JSON content."""

    def capabilities(self) -> ProviderCapabilities:
        """Declare the operations this configured model may perform."""

    def respond(self, turn: ProviderTurn) -> ProviderText:
        """Return normalized text for one turn."""

    def transcribe(self, audio: bytes, media_type: str, timeout: float) -> str:
        """Return a transcript. The transcript is derived text, not a verified fact."""

    def synthesize(self, text: str, timeout: float) -> tuple[bytes, str]:
        """Return generated audio bytes and a media type."""
