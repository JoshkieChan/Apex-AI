"""Attach governed memory to a provider request without giving it a database."""

from __future__ import annotations

from apex.contracts.schemas import ModelMessage, ModelRequest
from apex.memory.contracts import AssembledContext
from apex.memory.service import context_for_provider


def with_memory(request: ModelRequest, context: AssembledContext | None) -> ModelRequest:
    """Prepend the governing instruction and the data payload. Memory is not a system policy."""
    messages, payload = context_for_provider(context)
    if not messages:
        return request
    prefix = [ModelMessage(role=role, content=content) for role, content in messages]  # type: ignore[arg-type]
    return request.model_copy(
        update={"messages": [*prefix, *request.messages], "memory_context": payload}
    )
