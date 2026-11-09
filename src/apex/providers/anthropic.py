"""Optional Anthropic Messages adapter.

The request shape follows the structured-output example at
https://platform.claude.com/docs/en/build-with-claude/structured-outputs
as checked on 2026-11-08: `output_config.format.type = json_schema` and
`anthropic-version: 2026-10-21`. Live calls are not part of the test suite.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from apex.contracts.schemas import ModelRequest, ModelResponse, TokenUsage
from apex.providers.errors import ProviderError
from apex.providers.http import parse_json_object, post_json


class AnthropicAdapter:
    """Messages API adapter. System text is sent separately from chat turns."""

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.anthropic.com",
        version: str = "2026-10-21",
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ProviderError("authentication", "ANTHROPIC_API_KEY is not configured.")
        if not model:
            raise ProviderError("unavailable", "ANTHROPIC_MODEL is not configured.")
        self.model_name = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._version = version
        self._client = client or httpx.Client()
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def complete_structured(self, request: ModelRequest) -> ModelResponse:
        system, messages = _split_messages(request)
        body: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": 1024,
            "messages": messages,
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": request.json_schema,
                }
            },
        }
        if system:
            body["system"] = system
        started = time.perf_counter()
        payload = post_json(
            self._client,
            f"{self._base_url}/v1/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": self._version,
                "content-type": "application/json",
            },
            body=body,
            timeout=request.timeout_seconds,
        )
        raw_usage = payload.get("usage")
        usage = raw_usage if isinstance(raw_usage, dict) else {}
        input_tokens = _optional_int(usage.get("input_tokens"))
        output_tokens = _optional_int(usage.get("output_tokens"))
        total = None
        if input_tokens is not None and output_tokens is not None:
            total = input_tokens + output_tokens
        return ModelResponse(
            provider=self.name,
            model=str(payload.get("model") or self.model_name),
            content=_extract_text_json(payload),
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total,
            ),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def _split_messages(request: ModelRequest) -> tuple[str, list[dict[str, str]]]:
    system_parts: list[str] = []
    messages: list[dict[str, str]] = []
    for message in request.messages:
        if message.role == "system":
            system_parts.append(message.content)
            continue
        if message.role == "assistant":
            messages.append({"role": "assistant", "content": message.content})
        else:
            messages.append({"role": "user", "content": message.content})
    if not messages:
        messages.append({"role": "user", "content": "Respond using the schema."})
    return "\n".join(system_parts), messages


def _extract_text_json(payload: dict[str, Any]) -> dict[str, Any]:
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        raise ProviderError("invalid_output", "The model provider returned no content.")
    texts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            texts.append(text)
    if not texts:
        raise ProviderError("invalid_output", "The model provider returned no text content.")
    return parse_json_object("".join(texts))


def _optional_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    return None
