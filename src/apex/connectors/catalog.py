"""Allowlisted reference operations. This is not an HTTP client."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from apex.connectors.contracts import CatalogOperation
from apex.connectors.gates import enforce
from apex.connectors.reference import configure_device, read_device, read_item, write_note
from apex.errors import DomainError

API_ENDPOINT = "apex-reference://inventory"
DEVICE_ENDPOINT = "apex-device://reference"
REFERENCE_DEVICE_KEY = "reference-device"


class InventoryLookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    project_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class InventoryLookupOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    found: bool
    item: str
    quantity: int | None = None


class InventoryNoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    text: str = Field(min_length=1, max_length=500)
    operation_id: str = Field(min_length=8, max_length=64)
    project_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class InventoryNoteOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    applied: bool
    operation_id: str
    replayed: bool


class DeviceReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_key: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    project_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class DeviceReadOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_key: str
    label: str
    available: bool


class DeviceConfigureInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_key: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    label: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    project_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class DeviceConfigureOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_key: str
    label: str


def api_catalog() -> tuple[CatalogOperation, ...]:
    """The only API operations M7 will execute."""
    return (
        CatalogOperation(
            operation_key="lookup",
            description="Read one reference inventory item.",
            input_schema=InventoryLookupInput.model_json_schema(),
            output_schema=InventoryLookupOutput.model_json_schema(),
            side_effect="read",
            permission_key="reference.lookup",
            risk="low",
            idempotent=True,
            verification_kind="reference.lookup",
        ),
        CatalogOperation(
            operation_key="note",
            description="Store one note for a reference inventory item.",
            input_schema=InventoryNoteInput.model_json_schema(),
            output_schema=InventoryNoteOutput.model_json_schema(),
            side_effect="non_idempotent_write",
            permission_key="reference.note",
            risk="high",
            idempotent=False,
            verification_kind="reference.note",
        ),
    )


def device_catalog() -> tuple[CatalogOperation, ...]:
    """The only device operations M7 will execute."""
    return (
        CatalogOperation(
            operation_key="read",
            description="Read the reference device label.",
            input_schema=DeviceReadInput.model_json_schema(),
            output_schema=DeviceReadOutput.model_json_schema(),
            side_effect="read",
            permission_key="device.read",
            risk="low",
            idempotent=True,
            verification_kind="device.read",
        ),
        CatalogOperation(
            operation_key="configure",
            description="Set the reference device label.",
            input_schema=DeviceConfigureInput.model_json_schema(),
            output_schema=DeviceConfigureOutput.model_json_schema(),
            side_effect="idempotent_write",
            permission_key="device.configure",
            risk="medium",
            idempotent=True,
            verification_kind="device.configure",
        ),
    )


def input_model(kind: str, operation_key: str) -> type[BaseModel]:
    """Return the Apex input model for a reference operation."""
    models = {
        ("api", "lookup"): InventoryLookupInput,
        ("api", "note"): InventoryNoteInput,
        ("device", "read"): DeviceReadInput,
        ("device", "configure"): DeviceConfigureInput,
    }
    found = models.get((kind, operation_key))
    if found is None:
        raise DomainError("unsupported_operation", "This operation has no Apex adapter.")
    return cast(type[BaseModel], found)


def output_model(kind: str, operation_key: str) -> type[BaseModel]:
    """Return the Apex output model for a reference operation."""
    models = {
        ("api", "lookup"): InventoryLookupOutput,
        ("api", "note"): InventoryNoteOutput,
        ("device", "read"): DeviceReadOutput,
        ("device", "configure"): DeviceConfigureOutput,
    }
    found = models.get((kind, operation_key))
    if found is None:
        raise DomainError("unsupported_operation", "This operation has no Apex adapter.")
    return cast(type[BaseModel], found)


def reference_adapter(
    *,
    gate_key: str,
    kind: str,
    operation_key: str,
    permission: str,
    device_key: str | None,
) -> Any:
    """Build an adapter that checks the connection ceiling before it touches state."""

    def run(payload: Mapping[str, object], timeout_seconds: float) -> dict[str, object]:
        del timeout_seconds
        project = payload.get("project_key")
        project_key = project if isinstance(project, str) else None
        enforce(gate_key, permission, project_key, datetime.now(UTC))
        if kind == "api" and operation_key == "lookup":
            item = read_item(str(payload["item"]))
            if item is None:
                return {"found": False, "item": str(payload["item"]), "quantity": None}
            return {"found": True, "item": item["name"], "quantity": item["quantity"]}
        if kind == "api" and operation_key == "note":
            result = write_note(
                str(payload["item"]),
                str(payload["text"]),
                str(payload["operation_id"]),
            )
            return result
        if device_key is None or str(payload["device_key"]) != device_key:
            raise DomainError(
                "device_unavailable",
                "The device is not available on this connection.",
                dispatched=False,
            )
        if operation_key == "read":
            row = read_device(device_key)
            if row is None or not row["available"]:
                raise DomainError(
                    "device_unavailable",
                    "The device is not available.",
                    dispatched=False,
                )
            return {"device_key": device_key, "label": row["label"], "available": True}
        if operation_key == "configure":
            configured = configure_device(device_key, str(payload["label"]))
            return {"device_key": configured["device_key"], "label": configured["label"]}
        raise DomainError("unsupported_operation", "This operation has no Apex adapter.")

    return run
