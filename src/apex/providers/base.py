"""Provider interface. Adapters normalize errors and return schema-shaped dictionaries."""

from __future__ import annotations

from typing import Protocol

from apex.contracts.schemas import ModelRequest, ModelResponse


class ModelProvider(Protocol):
    """Structured completion. Callers validate `content` against their own schema."""

    name: str
    model_name: str

    def complete_structured(self, request: ModelRequest) -> ModelResponse:
        """Invoke the provider and return parsed JSON content."""
