"""Execution boundary, verification independence, and provider adapters."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

import httpx
import pytest
from sqlalchemy.orm import Session
from tests.unit.test_intent_plan_authority import _plan_step, _principal

from apex.agents.roles import Verifier
from apex.authority.engine import AuthorityEngine
from apex.capabilities.builtins import (
    TextAnalyzeInput,
    TextAnalyzeOutput,
    analyze_text,
    build_default_registry,
    export_record,
)
from apex.capabilities.registry import CapabilityRegistry, CapabilitySpec
from apex.clock import FrozenClock
from apex.contracts.schemas import ModelMessage, ModelRequest, RiskLevel, VerificationStatus
from apex.errors import DomainError
from apex.execution.engine import ExecutionEngine
from apex.persistence.models import PermissionGrant
from apex.persistence.repository import executions_for_step
from apex.providers.anthropic import AnthropicAdapter
from apex.providers.errors import ProviderError
from apex.providers.mock import MockModelProvider
from apex.providers.openai import OpenAIAdapter
from apex.runtime_ids import new_id
from apex.verification.engine import verify_execution


def test_duplicate_execution_calls_adapter_once(session: Session) -> None:
    clock = FrozenClock()
    principal = _principal(session, "executor")
    session.add(
        PermissionGrant(
            id=new_id(),
            principal_id=principal.id,
            permission_key="text.analyze",
            effect="allow",
            created_at=clock.now(),
        )
    )
    plan, step = _plan_step(session, principal.id, clock)
    calls = {"n": 0}

    def counting(payload: Mapping[str, object], timeout_seconds: float) -> dict[str, object]:
        calls["n"] += 1
        return analyze_text(payload, timeout_seconds)

    registry = CapabilityRegistry()
    registry.register(
        CapabilitySpec(
            id="text.analyze",
            name="Analyze text",
            description="count",
            permission_key="text.analyze",
            risk=RiskLevel.low,
            input_model=TextAnalyzeInput,
            output_model=TextAnalyzeOutput,
            adapter=counting,
            verification_kind="text.analyze",
        )
    )
    session.commit()
    engine = ExecutionEngine(session, registry, AuthorityEngine(session), clock)
    first = engine.execute(
        task_id=plan.task_id,
        principal_id=principal.id,
        step=step,
        approval=None,
        prior_outputs={},
        operation_hash=step.operation_hash,
    )
    second = engine.execute(
        task_id=plan.task_id,
        principal_id=principal.id,
        step=step,
        approval=None,
        prior_outputs={},
        operation_hash=step.operation_hash,
    )
    assert first.id == second.id
    assert calls["n"] == 1
    assert len(executions_for_step(session, step.id)) == 1
    assert first.output_payload is not None
    assert first.output_payload["word_count"] == 2


def test_unregistered_capability_and_default_deny(session: Session) -> None:
    clock = FrozenClock()
    principal = _principal(session, "nobody")
    plan, step = _plan_step(session, principal.id, clock)
    session.commit()
    engine = ExecutionEngine(session, build_default_registry(), AuthorityEngine(session), clock)
    step.capability_id = "os.system"
    with pytest.raises(DomainError) as missing:
        engine.execute(
            task_id=plan.task_id,
            principal_id=principal.id,
            step=step,
            approval=None,
            prior_outputs={},
            operation_hash=step.operation_hash,
        )
    assert missing.value.code == "unregistered_capability"
    step.capability_id = "text.analyze"
    with pytest.raises(DomainError) as denied:
        engine.execute(
            task_id=plan.task_id,
            principal_id=principal.id,
            step=step,
            approval=None,
            prior_outputs={},
            operation_hash=step.operation_hash,
        )
    assert denied.value.code == "permission_denied"


def test_verifier_ignores_a_passing_model_suggestion() -> None:
    provider = MockModelProvider(
        scripts={"VerificationNote": {"note": "I say this passed.", "suggested_status": "passed"}}
    )
    report = Verifier(provider).review(
        verification_kind="text.analyze",
        execution_status="succeeded",
        resolved_input={"text": "one two"},
        output={"word_count": 99, "character_count": 7, "preview": "one two"},
    )
    assert report.status == VerificationStatus.failed
    assert report.advisory_suggestion == VerificationStatus.passed
    assert report.advisory_disagrees is True


def test_export_receipt_is_recomputed_independently() -> None:
    content = {"note": "quarterly"}
    encoded = json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    receipt = f"exp_{hashlib.sha256(encoded).hexdigest()[:16]}"
    output = export_record({"destination_label": "finance-archive", "content": content}, 1.0)
    assert output["receipt_id"] == receipt
    assert output["byte_count"] == len(encoded)
    passed = verify_execution(
        verification_kind="records.export",
        execution_status="succeeded",
        resolved_input={"destination_label": "finance-archive", "content": content},
        output=output,
    )
    assert passed.status == VerificationStatus.passed
    forged = dict(output)
    forged["byte_count"] = 1
    failed = verify_execution(
        verification_kind="records.export",
        execution_status="succeeded",
        resolved_input={"destination_label": "finance-archive", "content": content},
        output=forged,
    )
    assert failed.status == VerificationStatus.failed


def test_openai_and_anthropic_adapters_parse_mocked_http() -> None:
    def openai_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"
        body = json.loads(request.content.decode())
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"summary":"ok","suggested_capabilities":[]}',
                        }
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    def anthropic_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"] == "2026-10-21"
        body = json.loads(request.content.decode())
        assert body["output_config"]["format"]["type"] == "json_schema"
        assert "Be precise." in body["system"]
        return httpx.Response(
            200,
            json={
                "model": "claude-test",
                "content": [
                    {"type": "text", "text": '{"summary":"ok","suggested_capabilities":[]}'}
                ],
                "usage": {"input_tokens": 4, "output_tokens": 1},
            },
        )

    request = ModelRequest(
        model="ignored",
        messages=[
            ModelMessage(role="system", content="Be precise."),
            ModelMessage(role="user", content="Hi"),
        ],
        schema_name="CoordinationNote",
        json_schema={"type": "object"},
    )
    openai = OpenAIAdapter(
        api_key="test-key",
        model="test-model",
        base_url="https://example.test/v1",
        client=httpx.Client(transport=httpx.MockTransport(openai_handler)),
    )
    parsed = openai.complete_structured(request)
    assert parsed.content["summary"] == "ok"
    assert parsed.usage.total_tokens == 5
    anthropic = AnthropicAdapter(
        api_key="test-key",
        model="claude-test",
        base_url="https://example.test",
        client=httpx.Client(transport=httpx.MockTransport(anthropic_handler)),
    )
    parsed = anthropic.complete_structured(request)
    assert parsed.usage.total_tokens == 5
    with pytest.raises(ProviderError):
        OpenAIAdapter(api_key="", model="test-model")


def test_provider_http_errors_are_normalized() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "nope"})

    adapter = OpenAIAdapter(
        api_key="test-key",
        model="test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderError) as exc:
        adapter.complete_structured(
            ModelRequest(
                model="m",
                messages=[ModelMessage(role="user", content="hi")],
                schema_name="Note",
                json_schema={"type": "object"},
            )
        )
    assert exc.value.code == "authentication"
    assert "nope" not in exc.value.message
