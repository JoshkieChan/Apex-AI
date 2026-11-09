"""Capability-specific checks. A successful adapter return is not verification."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from apex.contracts.schemas import (
    CheckResult,
    ExecutionStatus,
    VerificationReport,
    VerificationStatus,
)


def register_checker(kind: str, checker: Any) -> None:
    """Install a deterministic checker. Model notes cannot replace one."""
    _CHECKERS[kind] = checker


def verify_execution(
    *,
    verification_kind: str,
    execution_status: str,
    resolved_input: dict[str, Any] | None,
    output: dict[str, Any] | None,
) -> VerificationReport:
    """Return a report whose status comes only from deterministic checks."""
    checks: list[CheckResult] = []
    if execution_status != ExecutionStatus.succeeded.value:
        checks.append(
            CheckResult(
                name="execution_succeeded",
                passed=False,
                detail="The execution did not succeed.",
            )
        )
        return _report(checks)
    if not isinstance(output, dict) or not isinstance(resolved_input, dict):
        checks.append(
            CheckResult(
                name="output_present",
                passed=False,
                detail="The execution did not produce a structured output.",
            )
        )
        return _report(checks)
    checker = _CHECKERS.get(verification_kind)
    if checker is None:
        checks.append(
            CheckResult(
                name="checker_registered",
                passed=False,
                detail="No deterministic checker is registered for this capability.",
            )
        )
        return VerificationReport(
            status=VerificationStatus.inconclusive,
            checks=checks,
            summary="Verification is inconclusive because no deterministic checker exists.",
        )
    checks.extend(checker(resolved_input, output))
    return _report(checks)


def _report(checks: list[CheckResult]) -> VerificationReport:
    if not checks:
        return VerificationReport(
            status=VerificationStatus.inconclusive,
            checks=[],
            summary="Verification is inconclusive.",
        )
    if all(check.passed for check in checks):
        return VerificationReport(
            status=VerificationStatus.passed,
            checks=checks,
            summary="All deterministic checks passed.",
        )
    return VerificationReport(
        status=VerificationStatus.failed,
        checks=checks,
        summary="One or more deterministic checks failed.",
    )


def _check_text_analyze(payload: dict[str, Any], output: dict[str, Any]) -> list[CheckResult]:
    text = payload.get("text")
    checks: list[CheckResult] = []
    if not isinstance(text, str):
        return [CheckResult(name="text_present", passed=False, detail="Input text is missing.")]
    expected_words = len(text.split())
    expected_chars = len(text)
    preview = text[:80]
    checks.append(
        CheckResult(
            name="word_count",
            passed=output.get("word_count") == expected_words,
            detail=f"Expected {expected_words} words.",
        )
    )
    checks.append(
        CheckResult(
            name="character_count",
            passed=output.get("character_count") == expected_chars,
            detail=f"Expected {expected_chars} characters.",
        )
    )
    checks.append(
        CheckResult(
            name="preview",
            passed=output.get("preview") == preview
            and text.startswith(str(output.get("preview", ""))),
            detail="Preview must be the leading characters of the input.",
        )
    )
    return checks


def _check_export(payload: dict[str, Any], output: dict[str, Any]) -> list[CheckResult]:
    content = payload.get("content")
    label = payload.get("destination_label")
    if not isinstance(content, dict) or not isinstance(label, str):
        return [
            CheckResult(name="export_input", passed=False, detail="Export input is incomplete.")
        ]
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    expected_receipt = f"exp_{hashlib.sha256(encoded).hexdigest()[:16]}"
    return [
        CheckResult(
            name="receipt_id",
            passed=output.get("receipt_id") == expected_receipt,
            detail="Receipt must match the canonical content digest.",
        ),
        CheckResult(
            name="byte_count",
            passed=output.get("byte_count") == len(encoded),
            detail="Byte count must match the canonical content encoding.",
        ),
        CheckResult(
            name="destination_label",
            passed=output.get("destination_label") == label,
            detail="Destination label must match the authorized input.",
        ),
    ]


_CHECKERS = {
    "text.analyze": _check_text_analyze,
    "records.export": _check_export,
}


def assess_claim(*, evidence_present: bool, supported: bool | None, shared_model: bool) -> str:
    """A model claim without independent evidence is never passed."""
    if not evidence_present:
        return "inconclusive"
    if supported is False:
        return "failed"
    if shared_model or supported is None:
        return "partially_verified"
    return "passed"


def verify_artifact(*, exists: bool, digest: str | None, expected_digest: str | None) -> str:
    """Confirm an artifact by existence and digest. A missing digest is not success."""
    if not exists:
        return "failed"
    if not digest or not expected_digest:
        return "inconclusive"
    if digest != expected_digest:
        return "failed"
    return "passed"
