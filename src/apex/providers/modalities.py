"""Provider-neutral multimodal turns. Vendor payloads stay inside adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from apex.contracts.schemas import TokenUsage


@dataclass(frozen=True)
class ProviderCapabilities:
    """What the configured model is allowed to do. Flags are not inferred from the vendor name."""

    text: bool
    image_understanding: bool
    audio_transcription: bool
    audio_generation: bool


@dataclass(frozen=True)
class ContentBlob:
    """One normalized part handed to an adapter."""

    kind: Literal["text", "image", "transcript"]
    text: str = ""
    data: bytes = b""
    media_type: str = ""


@dataclass(frozen=True)
class ProviderTurn:
    """One model call. History is text only. Raw media from earlier turns is not replayed here."""

    policy: str
    history: tuple[str, ...]
    parts: tuple[ContentBlob, ...]
    timeout_seconds: float


@dataclass(frozen=True)
class ProviderText:
    """Normalized text. It is advisory until a deterministic check says otherwise."""

    text: str
    usage: TokenUsage
    latency_ms: int
