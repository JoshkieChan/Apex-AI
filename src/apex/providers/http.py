"""Shared HTTP error mapping for optional provider adapters."""

from __future__ import annotations

import json
from typing import Any

import httpx

from apex.providers.errors import ProviderError


def map_status(status_code: int) -> ProviderError:
    """Map an HTTP status to a normalized provider error without including the body."""
    if status_code in {401, 403}:
        return ProviderError("authentication", "The model provider rejected the credentials.")
    if status_code == 429:
        return ProviderError("rate_limit", "The model provider rate-limited the request.")
    if status_code == 408:
        return ProviderError("timeout", "The model provider timed out.")
    if status_code >= 500:
        return ProviderError("unavailable", "The model provider was unavailable.")
    return ProviderError("invalid_output", "The model provider rejected the request.")


def post_json(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    """POST JSON and return a decoded object. Response bodies are not included in errors."""
    try:
        response = client.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise ProviderError("timeout", "The model provider timed out.") from exc
    except httpx.HTTPError as exc:
        raise ProviderError("unavailable", "The model provider was unavailable.") from exc
    if response.status_code >= 400:
        raise map_status(response.status_code)
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise ProviderError("invalid_output", "The model provider returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise ProviderError("invalid_output", "The model provider returned an unexpected payload.")
    return payload


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from model text."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError("invalid_output", "The model output was not valid JSON.") from exc
    if not isinstance(value, dict):
        raise ProviderError("invalid_output", "The model output was not a JSON object.")
    return value
