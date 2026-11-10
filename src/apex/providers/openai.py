"""Optional OpenAI Chat Completions adapter.

The request shape follows the Chat Completions structured-output format
documented at https://developers.openai.com/api/docs/guides/structured-outputs
as checked on 2026-11-08: `response_format.type = json_schema`. Live calls are
not part of the test suite and require OPENAI_API_KEY plus OPENAI_MODEL.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import httpx

from apex.contracts.schemas import ModelRequest, ModelResponse, TokenUsage
from apex.providers.errors import ProviderError
from apex.providers.http import map_status, parse_json_object, post_json
from apex.providers.modalities import (
    ProviderCapabilities,
    ProviderText,
    ProviderTurn,
)


class OpenAIAdapter:
    """Chat Completions adapter. It does not read a key from source code."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        client: httpx.Client | None = None,
        image_understanding: bool = False,
        transcription: bool = False,
        transcription_model: str = "",
        speech: bool = False,
        speech_model: str = "",
        max_audio_bytes: int = 5_000_000,
    ) -> None:
        if not api_key:
            raise ProviderError("authentication", "OPENAI_API_KEY is not configured.")
        if not model:
            raise ProviderError("unavailable", "OPENAI_MODEL is not configured.")
        self.model_name = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client()
        self._owns_client = client is None
        self._image = image_understanding
        self._transcription = transcription
        self._transcription_model = transcription_model
        self._speech = speech
        self._speech_model = speech_model
        self._max_audio_bytes = max_audio_bytes

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            text=True,
            image_understanding=self._image,
            audio_transcription=self._transcription and bool(self._transcription_model),
            audio_generation=self._speech and bool(self._speech_model),
        )

    def respond(self, turn: ProviderTurn) -> ProviderText:
        """Text, with images only when this model is explicitly enabled for them."""
        if any(part.kind == "image" for part in turn.parts) and not self._image:
            raise ProviderError("unsupported", "This model is not configured for images.")
        content: list[dict[str, Any]] = []
        if turn.policy:
            content.append({"type": "text", "text": turn.policy})
        for line in turn.history:
            content.append({"type": "text", "text": line})
        for part in turn.parts:
            if part.kind == "image":
                encoded = base64.b64encode(part.data).decode("ascii")
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{part.media_type};base64,{encoded}"},
                    }
                )
            else:
                content.append({"type": "text", "text": part.text})
        started = time.perf_counter()
        payload = post_json(
            self._client,
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            body={"model": self.model_name, "messages": [{"role": "user", "content": content}]},
            timeout=turn.timeout_seconds,
        )
        return ProviderText(
            text=_extract_text(payload),
            usage=_usage(payload),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def transcribe(self, audio: bytes, media_type: str, timeout: float) -> str:
        if not self.capabilities().audio_transcription:
            raise ProviderError("unsupported", "This model is not configured for transcription.")
        name = "audio.wav" if media_type == "audio/wav" else "audio.mp3"
        try:
            response = self._client.post(
                f"{self._base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                data={"model": self._transcription_model},
                files={"file": (name, audio, media_type)},
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", "The model provider timed out.") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("unavailable", "The model provider was unavailable.") from exc
        if response.status_code >= 400:
            raise map_status(response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("invalid_output", "The transcription was not JSON.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise ProviderError("invalid_output", "The transcription had no text.")
        text = payload["text"].strip()
        if not text:
            raise ProviderError("invalid_output", "The transcription was empty.")
        return text

    def synthesize(self, text: str, timeout: float) -> tuple[bytes, str]:
        if not self.capabilities().audio_generation:
            raise ProviderError("unsupported", "This model is not configured for speech.")
        try:
            response = self._client.post(
                f"{self._base_url}/audio/speech",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._speech_model,
                    "input": text,
                    "voice": "alloy",
                    "response_format": "wav",
                },
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", "The model provider timed out.") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("unavailable", "The model provider was unavailable.") from exc
        if response.status_code >= 400:
            raise map_status(response.status_code)
        audio = response.content
        if len(audio) > self._max_audio_bytes or not audio.startswith(b"RIFF"):
            raise ProviderError("invalid_output", "The speech response was not usable audio.")
        return audio, "audio/wav"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def complete_structured(self, request: ModelRequest) -> ModelResponse:
        body = {
            "model": self.model_name,
            "messages": [message.model_dump() for message in request.messages],
            "temperature": request.temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name[:64],
                    "strict": True,
                    "schema": request.json_schema,
                },
            },
        }
        started = time.perf_counter()
        payload = post_json(
            self._client,
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            body=body,
            timeout=request.timeout_seconds,
        )
        content = _extract_content(payload)
        raw_usage = payload.get("usage")
        usage = raw_usage if isinstance(raw_usage, dict) else {}
        return ModelResponse(
            provider=self.name,
            model=str(payload.get("model") or self.model_name),
            content=content,
            usage=TokenUsage(
                input_tokens=_optional_int(usage.get("prompt_tokens")),
                output_tokens=_optional_int(usage.get("completion_tokens")),
                total_tokens=_optional_int(usage.get("total_tokens")),
            ),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def _extract_content(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError("invalid_output", "The model provider returned no choices.")
    first = choices[0]
    if not isinstance(first, dict):
        raise ProviderError("invalid_output", "The model provider returned an unexpected choice.")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ProviderError("invalid_output", "The model provider returned no message.")
    if message.get("refusal"):
        raise ProviderError("invalid_output", "The model provider refused the request.")
    content = message.get("content")
    if not isinstance(content, str):
        raise ProviderError("invalid_output", "The model provider returned no text content.")
    return parse_json_object(content)


def _extract_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderError("invalid_output", "The model provider returned no choices.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ProviderError("invalid_output", "The model provider returned no message.")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("invalid_output", "The model provider returned no text content.")
    if len(content) > 16_000:
        raise ProviderError("invalid_output", "The model provider returned an unbounded response.")
    return content


def _usage(payload: dict[str, Any]) -> TokenUsage:
    raw = payload.get("usage")
    usage = raw if isinstance(raw, dict) else {}
    return TokenUsage(
        input_tokens=_optional_int(usage.get("prompt_tokens")),
        output_tokens=_optional_int(usage.get("completion_tokens")),
        total_tokens=_optional_int(usage.get("total_tokens")),
    )


def _optional_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    return None
