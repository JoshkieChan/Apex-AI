"""Deterministic readback for the reference connector and device."""

from __future__ import annotations

from typing import Any

from apex.connectors.reference import read_device, read_item, read_note
from apex.contracts.schemas import CheckResult
from apex.verification.engine import register_checker


def install_connector_checks() -> None:
    """Register checkers that read the reference stores, not the model text."""
    register_checker("reference.lookup", _lookup)
    register_checker("reference.note", _note)
    register_checker("device.read", _device_read)
    register_checker("device.configure", _device_configure)


def _lookup(payload: dict[str, Any], output: dict[str, Any]) -> list[CheckResult]:
    name = payload.get("item")
    if not isinstance(name, str):
        return [CheckResult(name="item_present", passed=False, detail="The item name is missing.")]
    stored = read_item(name)
    found = stored is not None
    quantity = None if stored is None else stored["quantity"]
    return [
        CheckResult(
            name="found",
            passed=output.get("found") is found,
            detail="The lookup result must match the reference inventory.",
        ),
        CheckResult(
            name="quantity",
            passed=output.get("quantity") == quantity,
            detail="The quantity must match the reference inventory.",
        ),
    ]


def _note(payload: dict[str, Any], output: dict[str, Any]) -> list[CheckResult]:
    operation_id = payload.get("operation_id")
    if not isinstance(operation_id, str):
        return [
            CheckResult(
                name="operation_present",
                passed=False,
                detail="The operation id is missing.",
            )
        ]
    stored = read_note(operation_id)
    return [
        CheckResult(
            name="stored_note",
            passed=stored is not None
            and stored["name"] == payload.get("item")
            and stored["text"] == payload.get("text")
            and output.get("applied") is True,
            detail="The reference store must contain the requested note.",
        )
    ]


def _device_read(payload: dict[str, Any], output: dict[str, Any]) -> list[CheckResult]:
    device_key = payload.get("device_key")
    if not isinstance(device_key, str):
        return [
            CheckResult(
                name="device_present",
                passed=False,
                detail="The device key is missing.",
            )
        ]
    stored = read_device(device_key)
    return [
        CheckResult(
            name="label",
            passed=stored is not None and output.get("label") == stored["label"],
            detail="The device label must match the reference device.",
        )
    ]


def _device_configure(payload: dict[str, Any], output: dict[str, Any]) -> list[CheckResult]:
    device_key = payload.get("device_key")
    label = payload.get("label")
    if not isinstance(device_key, str) or not isinstance(label, str):
        return [
            CheckResult(
                name="input_present",
                passed=False,
                detail="The device input is incomplete.",
            )
        ]
    stored = read_device(device_key)
    return [
        CheckResult(
            name="stored_label",
            passed=stored is not None and stored["label"] == label and output.get("label") == label,
            detail="The reference device must store the requested label.",
        )
    ]
