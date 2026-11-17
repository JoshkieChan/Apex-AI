"""Capability flags decide whether an adapter may receive a modality."""

from __future__ import annotations

import httpx
import pytest

from apex.interaction.media import silence_wav
from apex.providers.anthropic import AnthropicAdapter
from apex.providers.errors import ProviderError
from apex.providers.modalities import ContentBlob, ProviderTurn
from apex.providers.openai import OpenAIAdapter


def test_openai_image_and_audio_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    del monkeypatch
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": "heard"})
        if request.url.path.endswith("/audio/speech"):
            return httpx.Response(200, content=silence_wav())
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "seen"}}], "usage": {"total_tokens": 3}},
        )

    disabled = OpenAIAdapter(
        api_key="test-key",
        model="gpt-test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert disabled.capabilities().image_understanding is False
    turn = ProviderTurn(
        policy="policy",
        history=(),
        parts=(ContentBlob(kind="image", data=b"png", media_type="image/png"),),
        timeout_seconds=1,
    )
    with pytest.raises(ProviderError) as denied:
        disabled.respond(turn)
    assert denied.value.code == "unsupported"
    assert calls == []
    enabled = OpenAIAdapter(
        api_key="test-key",
        model="gpt-test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        image_understanding=True,
        transcription=True,
        transcription_model="whisper-1",
        speech=True,
        speech_model="tts-1",
    )
    assert enabled.respond(turn).text == "seen"
    assert enabled.transcribe(b"audio", "audio/wav", 1) == "heard"
    audio, media_type = enabled.synthesize("hello", 1)
    assert media_type == "audio/wav" and audio.startswith(b"RIFF")
    assert "/chat/completions" in calls[0]


def test_anthropic_does_not_silently_gain_audio() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        return httpx.Response(200, json={"content": [{"type": "text", "text": "described"}]})

    adapter = AnthropicAdapter(
        api_key="test-key",
        model="claude-test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        image_understanding=True,
    )
    assert adapter.capabilities().audio_transcription is False
    text = adapter.respond(
        ProviderTurn(
            policy="policy",
            history=(),
            parts=(ContentBlob(kind="image", data=b"png", media_type="image/png"),),
            timeout_seconds=1,
        )
    )
    assert text.text == "described"
    with pytest.raises(ProviderError) as blocked:
        adapter.transcribe(b"audio", "audio/wav", 1)
    assert blocked.value.code == "unsupported"


def test_anthropic_invalid_text_is_a_confirmed_response() -> None:
    adapter = AnthropicAdapter(
        api_key="test-key",
        model="claude-test",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"content": []})
            )
        ),
    )
    with pytest.raises(ProviderError) as caught:
        adapter.respond(ProviderTurn(policy="policy", history=(), parts=(), timeout_seconds=1))
    assert caught.value.sent is True
    assert caught.value.confirmed is True


def test_connect_timeout_is_not_sent() -> None:
    from apex.providers.http import post_json

    def raise_connect(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("name lookup failed")

    client = httpx.Client(transport=httpx.MockTransport(raise_connect))
    with pytest.raises(ProviderError) as caught:
        post_json(
            client,
            "https://example.test/v1",
            headers={},
            body={},
            timeout=1,
        )
    assert caught.value.sent is False
    assert caught.value.confirmed is False
