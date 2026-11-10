"""In-process reference systems. They do not open a socket or read the host."""

from __future__ import annotations

from typing import Any

from apex.errors import DomainError

_ITEMS: dict[str, dict[str, Any]] = {}
_NOTES: dict[str, dict[str, str]] = {}
_DEVICES: dict[str, dict[str, Any]] = {}
_FAIL_NOTE_AFTER_WRITE = False


def reset_reference() -> None:
    """Clear fixture state, including a one-shot write failure."""
    global _FAIL_NOTE_AFTER_WRITE
    _ITEMS.clear()
    _NOTES.clear()
    _DEVICES.clear()
    _FAIL_NOTE_AFTER_WRITE = False


def seed_item(name: str, quantity: int) -> None:
    """Insert one inventory row for a test or a local demonstration."""
    _ITEMS[name] = {"name": name, "quantity": quantity}


def read_item(name: str) -> dict[str, Any] | None:
    """Return a copy of one inventory row."""
    row = _ITEMS.get(name)
    if row is None:
        return None
    return {"name": row["name"], "quantity": row["quantity"]}


def write_note(name: str, text: str, operation_id: str) -> dict[str, Any]:
    """Apply one note. The same operation id does not append a second note."""
    prior = _NOTES.get(operation_id)
    if prior is not None:
        if prior["name"] != name or prior["text"] != text:
            raise DomainError(
                "idempotency_conflict",
                "This operation id was already used for different content.",
                dispatched=False,
            )
        return {"applied": True, "operation_id": operation_id, "replayed": True}
    if name not in _ITEMS:
        raise DomainError("not_found", "The inventory item does not exist.", dispatched=False)
    _NOTES[operation_id] = {"name": name, "text": text}
    if _FAIL_NOTE_AFTER_WRITE:
        raise DomainError(
            "outcome_uncertain",
            "The note may have been stored before the response was lost.",
            dispatched=True,
        )
    return {"applied": True, "operation_id": operation_id, "replayed": False}


def read_note(operation_id: str) -> dict[str, str] | None:
    """Return the stored note for an operation id."""
    row = _NOTES.get(operation_id)
    if row is None:
        return None
    return {"name": row["name"], "text": row["text"]}


def fail_note_after_write() -> None:
    """The next new note is stored and then reported as uncertain."""
    global _FAIL_NOTE_AFTER_WRITE
    _FAIL_NOTE_AFTER_WRITE = True


def remember_device(device_key: str) -> None:
    """Create an unpaired device record in the reference store."""
    _DEVICES[device_key] = {"label": "", "paired": False, "available": False}


def mark_device_paired(device_key: str) -> None:
    """Record a successful pair. This does not authorize an operation."""
    row = _DEVICES.get(device_key)
    if row is None:
        raise DomainError("device_unavailable", "The device is not registered.", dispatched=False)
    row["paired"] = True
    row["available"] = True


def mark_device_unavailable(device_key: str) -> None:
    """The device is disconnected. Stored configuration is left in place."""
    row = _DEVICES.get(device_key)
    if row is None:
        return
    row["available"] = False


def read_device(device_key: str) -> dict[str, Any] | None:
    """Return device state. Unavailable is still a row, not a missing device."""
    row = _DEVICES.get(device_key)
    if row is None:
        return None
    return {
        "device_key": device_key,
        "label": row["label"],
        "paired": row["paired"],
        "available": row["available"],
    }


def configure_device(device_key: str, label: str) -> dict[str, str]:
    """Set the label when the device is paired and available."""
    row = _DEVICES.get(device_key)
    if row is None or not row["paired"] or not row["available"]:
        raise DomainError(
            "device_unavailable",
            "The device is not available.",
            dispatched=False,
        )
    row["label"] = label
    return {"device_key": device_key, "label": label}
