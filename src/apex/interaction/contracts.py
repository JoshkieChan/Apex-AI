"""Interaction contracts. These types do not carry provider SDK objects."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from apex.contracts.schemas import ModelConfig, TokenUsage

Provenance = Literal["user", "derived", "model", "system"]
Trust = Literal["untrusted", "derived", "advisory"]
OutputModality = Literal["text", "audio"]


class TextInput(ModelConfig):
    kind: Literal["text"]
    text: str = Field(min_length=1, max_length=8000)


class ImageInput(ModelConfig):
    kind: Literal["image"]
    media_id: str = Field(min_length=36, max_length=36)


class AudioInput(ModelConfig):
    kind: Literal["audio"]
    media_id: str = Field(min_length=36, max_length=36)


InteractionInput = Annotated[TextInput | ImageInput | AudioInput, Field(discriminator="kind")]


def _text_modality() -> list[OutputModality]:
    return ["text"]


class InteractionCreate(ModelConfig):
    """A turn. The caller cannot name the principal."""

    conversation_id: str | None = None
    project_key: str | None = Field(default=None, max_length=64)
    task_id: str | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    retain_media: bool = False
    output_modalities: list[OutputModality] = Field(default_factory=_text_modality, max_length=2)
    parts: list[InteractionInput] = Field(min_length=1, max_length=8)


class TextOutput(ModelConfig):
    kind: Literal["text", "transcript"]
    text: str
    provenance: Provenance
    trust: Trust
    source_media_id: str | None = None


class AudioOutput(ModelConfig):
    kind: Literal["audio"]
    media_id: str
    media_type: str
    provenance: Provenance
    trust: Trust


InteractionOutput = Annotated[TextOutput | AudioOutput, Field(discriminator="kind")]


class InteractionView(ModelConfig):
    id: str
    conversation_id: str
    status: Literal["completed", "failed"]
    parts: list[InteractionOutput]
    provider: str
    model: str
    usage: TokenUsage
    latency_ms: int
    error_code: str | None = None
    speech_error: str | None = None
    created_at: datetime


class MediaView(ModelConfig):
    id: str
    kind: Literal["image", "audio"]
    media_type: str
    byte_size: int
    sha256: str
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    provenance: Provenance
    trust: Trust
    expires_at: datetime


class ConversationView(ModelConfig):
    id: str
    project_key: str | None
    interactions: list[InteractionView]
