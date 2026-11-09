"""Optional OpenAI Chat Completions adapter.

The request shape follows the Chat Completions structured-output format
documented at https://developers.openai.com/api/docs/guides/structured-outputs
as checked on 2026-11-08: `response_format.type = json_schema`. Live calls are
not part of the test suite and require OPENAI_API_KEY plus OPENAI_MODEL.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from apex.contracts.schemas import ModelRequest, ModelResponse, TokenUsage
from apex.providers.errors import ProviderError
from apex.providers.http import parse_json_object, post_json


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


def _optional_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    return None
