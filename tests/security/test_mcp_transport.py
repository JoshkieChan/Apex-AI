"""MCP SDK 2.3.0 transport boundary. The injected client is the only MCP socket path."""

from __future__ import annotations

import asyncio

import httpx2
import pytest
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import request_within_origin

from apex.config import Settings
from apex.errors import DomainError
from apex.mcp.remote import MCP_MAX_REDIRECTS, _PinnedTransport
from apex.netpolicy import validate_remote_url


def test_zero_redirect_budget_does_not_follow_a_redirect() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(307, headers={"location": "https://evil.example/steal"})

    async def run() -> int:
        transport = httpx2.MockTransport(handler)
        async with httpx2.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            max_redirects=MCP_MAX_REDIRECTS,
        ) as client:
            response = await request_within_origin(client, "POST", "https://mcp.example/mcp")
            return response.status_code

    assert asyncio.run(run()) == 307


def test_proxy_environment_is_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.host)
        return httpx2.Response(204)

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")

    async def run() -> None:
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
            max_redirects=0,
        ) as client:
            await client.post("https://mcp.example/mcp")

    asyncio.run(run())
    assert seen == ["mcp.example"]


def test_sdk_does_not_open_its_own_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> httpx2.AsyncClient:
        raise AssertionError("SDK opened its own client")

    monkeypatch.setattr("mcp.client.streamable_http.create_mcp_http_client", boom)

    async def run() -> None:
        transport = httpx2.MockTransport(lambda _request: httpx2.Response(406))
        async with httpx2.AsyncClient(
            transport=transport, trust_env=False, max_redirects=0
        ) as client:
            async with streamable_http_client("https://mcp.example/mcp", http_client=client):
                return

    asyncio.run(run())


def test_pinned_transport_rejects_a_different_host_and_a_new_address() -> None:
    def resolver(_host: str, _port: int) -> list[str]:
        return ["8.8.8.8"]

    transport = _PinnedTransport(
        host="mcp.example",
        port=443,
        addresses=["1.1.1.1"],
        allow_loopback=False,
        max_bytes=1000,
        resolver=resolver,
        verify=False,
        inner=httpx2.MockTransport(lambda _request: httpx2.Response(204)),
    )
    foreign = httpx2.Request("POST", "https://evil.example/mcp")
    with pytest.raises(DomainError, match="redirected"):
        asyncio.run(transport.handle_async_request(foreign))
    changed = httpx2.Request("POST", "https://mcp.example/mcp")
    with pytest.raises(DomainError, match="address changed"):
        asyncio.run(transport.handle_async_request(changed))


def test_production_remote_mcp_requires_https() -> None:
    settings = Settings(_env_file=None, environment="production", model_provider="mock")
    with pytest.raises(DomainError, match="not acceptable"):
        validate_remote_url("http://mcp.example/mcp", settings, allow_local_fixture=False)
    allowed = validate_remote_url(
        "https://mcp.example/mcp",
        settings,
        allow_local_fixture=False,
        resolver=lambda _host, _port: ["1.1.1.1"],
    )
    assert allowed.startswith("https://")
