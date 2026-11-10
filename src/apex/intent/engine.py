"""Classify a request into capabilities and typed inputs.

The supported utterances are intentionally narrow. A paraphrase becomes a
clarification instead of a guessed capability.
"""

from __future__ import annotations

import hashlib
import json
import re

from apex.capabilities.registry import CapabilityRegistry
from apex.contracts.schemas import (
    Intent,
    IntentDraft,
    ModelMessage,
    ModelRequest,
    RiskLevel,
)
from apex.logging_config import get_logger
from apex.mcp.fixture import LOOKUP_ID, UPDATE_ID
from apex.memory.contracts import AssembledContext
from apex.providers.base import ModelProvider
from apex.providers.context import with_memory
from apex.redaction import safe_identifier

logger = get_logger("intent")

_ANALYZE = re.compile(
    r"^(?:Analyze the following text and report its word count:"
    r"|Count the words in the following text:)\s*(?P<text>.+)$",
    re.DOTALL,
)
_EXPORT = re.compile(
    r"^Export a record to the archive labeled "
    r"(?P<label>[A-Za-z0-9][A-Za-z0-9_-]{0,63}) with content (?P<body>\{.*\})$",
    re.DOTALL,
)
_LOOKUP = re.compile(r"^Look up fixture record (?P<name>[A-Za-z0-9][A-Za-z0-9_-]{0,63})$")
_INVENTORY = re.compile(
    r"^Look up reference inventory item (?P<item>[A-Za-z0-9][A-Za-z0-9_-]{0,63})"
    r"(?: for project (?P<project>[A-Za-z0-9_-]{1,64}))?$"
)
_DEVICE_LABEL = re.compile(
    r"^Set reference device label to (?P<label>[A-Za-z0-9][A-Za-z0-9_-]{0,63})$"
)
_NOTE = re.compile(
    r"^Record reference note '(?P<text>[^']{1,80})' on item "
    r"(?P<item>[A-Za-z0-9][A-Za-z0-9_-]{0,63}) as operation "
    r"(?P<operation>[A-Za-z0-9]{8,64})$"
)
_UPDATE = re.compile(
    r"^Update fixture record (?P<name>[A-Za-z0-9][A-Za-z0-9_-]{0,63}) "
    r"status to (?P<status>[A-Za-z0-9][A-Za-z0-9_-]{0,63})$"
)
_COMBINED = re.compile(
    r"^Count the words in '(?P<text>[^']+)' and export the analysis "
    r"to the archive labeled (?P<label>[A-Za-z0-9][A-Za-z0-9_-]{0,63})$",
    re.DOTALL,
)


class IntentEngine:
    """Produce an intent. Capability selection stays on the deterministic parse."""

    def __init__(self, registry: CapabilityRegistry, provider: ModelProvider | None = None) -> None:
        self._registry = registry
        self._provider = provider

    def interpret(self, text: str, *, memory: AssembledContext | None = None) -> Intent:
        intent = _deterministic(text.strip(), self._registry)
        if self._provider is None:
            return intent
        draft, dropped = self._advisory(text, memory)
        if draft is None:
            return intent
        return intent.model_copy(
            update={
                "source": "deterministic+model",
                "model_objective": draft.objective,
                "dropped_model_capabilities": dropped,
            }
        )

    def _advisory(
        self, text: str, memory: AssembledContext | None
    ) -> tuple[IntentDraft | None, list[str]]:
        provider = self._provider
        if provider is None:
            return None, []
        request = with_memory(
            ModelRequest(
                model=provider.model_name,
                messages=[
                    ModelMessage(
                        role="system",
                        content=(
                            "Classify the request. You cannot grant permission "
                            "or choose unregistered tools."
                        ),
                    ),
                    ModelMessage(role="user", content=text),
                ],
                schema_name="IntentDraft",
                json_schema=IntentDraft.model_json_schema(),
            ),
            memory,
        )
        try:
            response = provider.complete_structured(request)
            draft = IntentDraft.model_validate(response.content)
        except Exception as exc:
            logger.info("model advisory ignored: %s", type(exc).__name__)
            return None, []
        dropped: list[str] = []
        for capability_id in draft.required_capabilities:
            spec = self._registry.get(capability_id)
            if spec is None or not spec.available:
                dropped.append(safe_identifier(capability_id))
        return draft, dropped


def _deterministic(text: str, registry: CapabilityRegistry) -> Intent:
    combined = _COMBINED.match(text)
    if combined is not None:
        body = combined.group("text")
        label = combined.group("label")
        return Intent(
            objective=f"Count words in the supplied text and export the analysis to {label}.",
            required_capabilities=["text.analyze", "records.export"],
            constraints=["Export content is the analysis output, not free-form model text."],
            risk=RiskLevel.high,
            expected_outcome="A word count and a simulated export receipt.",
            sensitivity_flags=["external-transfer-label"],
            inputs={
                "text.analyze": {"text": body},
                "records.export": {"destination_label": label},
            },
            bindings={"records.export": {"content": {"step_index": 0}}},
            source="deterministic",
        )
    exported = _EXPORT.match(text)
    if exported is not None:
        try:
            content = json.loads(exported.group("body"))
        except json.JSONDecodeError:
            return _clarification(["Export content must be a JSON object."])
        if not isinstance(content, dict):
            return _clarification(["Export content must be a JSON object."])
        label = exported.group("label")
        return Intent(
            objective=f"Export a record to the archive labeled {label}.",
            required_capabilities=["records.export"],
            constraints=["The export is a simulated receipt and does not leave the process."],
            risk=RiskLevel.high,
            expected_outcome="A simulated export receipt bound to the supplied content.",
            sensitivity_flags=["external-transfer-label"],
            inputs={"records.export": {"destination_label": label, "content": content}},
            source="deterministic",
        )
    updated = _UPDATE.match(text)
    if updated is not None:
        return _mcp_intent(
            registry,
            capability_id=UPDATE_ID,
            permission_key="records.update",
            objective=f"Update fixture record {updated.group('name')}.",
            risk=RiskLevel.high,
            expected="The fixture record status matches the request.",
            payload={
                "name": updated.group("name"),
                "status": updated.group("status"),
                "operation_id": hashlib.sha256(
                    f"{updated.group('name')}:{updated.group('status')}".encode()
                ).hexdigest()[:32],
            },
        )
    inventory = _INVENTORY.match(text)
    if inventory is not None:
        payload = {"item": inventory.group("item")}
        if inventory.group("project"):
            payload["project_key"] = inventory.group("project")
        return _mcp_intent(
            registry,
            capability_id="api.reference.lookup",
            permission_key="reference.lookup",
            objective=f"Look up reference inventory item {inventory.group('item')}.",
            risk=RiskLevel.low,
            expected="The reference inventory item matches the stored row.",
            payload=payload,
        )
    noted = _NOTE.match(text)
    if noted is not None:
        return _mcp_intent(
            registry,
            capability_id="api.reference.note",
            permission_key="reference.note",
            objective="Store one reference inventory note.",
            risk=RiskLevel.high,
            expected="The reference inventory stores the note once.",
            payload={
                "item": noted.group("item"),
                "text": noted.group("text"),
                "operation_id": noted.group("operation"),
            },
        )
    labeled = _DEVICE_LABEL.match(text)
    if labeled is not None:
        return _mcp_intent(
            registry,
            capability_id="device.reference-device.configure",
            permission_key="device.configure",
            objective="Set the reference device label.",
            risk=RiskLevel.medium,
            expected="The reference device stores the requested label.",
            payload={"device_key": "reference-device", "label": labeled.group("label")},
        )
    looked = _LOOKUP.match(text)
    if looked is not None:
        return _mcp_intent(
            registry,
            capability_id=LOOKUP_ID,
            permission_key="records.lookup",
            objective=f"Look up fixture record {looked.group('name')}.",
            risk=RiskLevel.low,
            expected="The fixture record is returned and matches stored state.",
            payload={"name": looked.group("name")},
        )
    analyzed = _ANALYZE.match(text)
    if analyzed is not None:
        body = analyzed.group("text").strip()
        if not body:
            return _clarification(["The text to analyze is missing."])
        return Intent(
            objective="Count the words and characters in the supplied text.",
            required_capabilities=["text.analyze"],
            constraints=["Use the deterministic text.analyze capability."],
            risk=RiskLevel.low,
            expected_outcome="A structured word count, character count, and preview.",
            inputs={"text.analyze": {"text": body}},
            source="deterministic",
        )
    return _clarification(
        [
            "The request does not match a registered capability utterance.",
            "Ask for a word count with 'Analyze the following text and report its word count: ...'",
            "or an export with 'Export a record to the archive labeled "
            "<label> with content {...}'.",
        ]
    )


def _mcp_intent(
    registry: CapabilityRegistry,
    *,
    capability_id: str,
    permission_key: str,
    objective: str,
    risk: RiskLevel,
    expected: str,
    payload: dict[str, str],
) -> Intent:
    resolved = _registered_capability(registry, capability_id, permission_key)
    if resolved is None:
        return _clarification(["That capability is not registered."])
    capability_id = resolved
    return Intent(
        objective=objective,
        required_capabilities=[capability_id],
        constraints=["Use the registered capability through the execution engine."],
        risk=risk,
        expected_outcome=expected,
        inputs={capability_id: payload},
        source="deterministic",
    )


def _registered_capability(
    registry: CapabilityRegistry,
    capability_id: str,
    permission_key: str,
) -> str | None:
    """Prefer the fixture id. Otherwise use the only registered capability for that permission."""
    spec = registry.get(capability_id)
    if spec is not None and spec.available:
        return capability_id
    matches = [
        item.id
        for item in registry.list()
        if item.available and item.permission_key == permission_key
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _clarification(missing: list[str]) -> Intent:
    return Intent(
        objective="Clarify the request before any capability is used.",
        required_capabilities=[],
        risk=RiskLevel.unknown,
        expected_outcome="A clarification response and no execution.",
        missing_information=missing,
        clarification_required=True,
        source="deterministic",
    )
