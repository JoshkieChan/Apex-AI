"""Remote MCP over the SDK's Streamable HTTP transport. Discovery is not permission."""

from __future__ import annotations

import asyncio
import os
import ssl
import threading
from typing import Any
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from apex.config import Settings
from apex.errors import DomainError
from apex.netpolicy import (
    Resolver,
    assert_safe_addresses,
    resolve_host,
    validate_remote_url,
)
from apex.persistence.models import McpServerRecord
from apex.secrets import resolve_configured_secret

_SLOTS = threading.BoundedSemaphore(4)
MCP_MAX_REDIRECTS = 0
_TOOL_CALL = b"tools/call"


class _ToolDispatch:
    """Whether the tools/call request was handed to the network."""

    def __init__(self) -> None:
        self.sent = False


def list_remote_tools(server: McpServerRecord, settings: Settings) -> list[dict[str, Any]]:
    """List tools on a registered remote server. The result is not executable."""
    if server.transport != "http" or not server.endpoint:
        raise DomainError("unsupported_server", "The MCP server has no remote endpoint.")
    return _run(
        server.endpoint,
        server.credential_ref,
        settings,
        _list,
    )


def invoke_remote_tool(
    *,
    endpoint: str,
    credential_ref: str | None,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_seconds: float,
    settings: Settings,
    resolver: Resolver | None = None,
) -> dict[str, Any]:
    """Call one remote tool. The caller is the execution engine, not the remote server."""

    async def _call(client: Client) -> dict[str, Any]:
        result = await client.call_tool(
            tool_name,
            arguments,
            read_timeout_seconds=timeout_seconds,
        )
        return _structured(result)

    return _run(endpoint, credential_ref, settings, _call, resolver=resolver)


def _run(
    endpoint: str,
    credential_ref: str | None,
    settings: Settings,
    operation: Any,
    resolver: Resolver | None = None,
) -> Any:
    if not _SLOTS.acquire(blocking=False):
        raise DomainError("capability_unavailable", "The MCP connection limit has been reached.")
    dispatch = _ToolDispatch()
    try:
        return asyncio.run(
            _session(endpoint, credential_ref, settings, operation, resolver, dispatch)
        )
    except DomainError:
        raise
    except Exception as exc:
        raise _transport_error(exc, sent=dispatch.sent) from exc
    finally:
        _SLOTS.release()


def _transport_error(exc: Exception, *, sent: bool) -> DomainError:
    """Classify a transport failure by whether the tool request was sent.

    Connection and pool failures before tools/call are definitive. A read,
    write, or protocol failure after that request was handed to the network
    is ambiguous: the server may already have applied it. httpx retries are
    disabled on the pinned transport, and this function does not call again.
    """
    pre_dispatch = isinstance(exc, (httpx2.ConnectTimeout, httpx2.ConnectError, httpx2.PoolTimeout))
    if sent and not pre_dispatch:
        code = (
            "endpoint_timeout"
            if isinstance(exc, httpx2.TimeoutException)
            else "capability_unavailable"
        )
        return DomainError(
            code,
            "The remote tool request was sent and the outcome was not confirmed.",
            dispatched=True,
        )
    if isinstance(exc, httpx2.TimeoutException):
        return DomainError(
            "endpoint_timeout",
            "The endpoint did not respond in time.",
            dispatched=False,
        )
    return DomainError(
        "capability_unavailable",
        "The MCP server did not complete the call.",
        dispatched=False,
    )


async def _session(
    endpoint: str,
    credential_ref: str | None,
    settings: Settings,
    operation: Any,
    resolver: Resolver | None,
    dispatch: _ToolDispatch,
) -> Any:
    url = validate_remote_url(
        endpoint,
        settings,
        allow_local_fixture=settings.mcp_allow_local_fixture,
        resolver=resolver,
    )
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    local = settings.mcp_allow_local_fixture and settings.environment in {"development", "test"}
    addresses = resolve_host(host, port, resolver)
    assert_safe_addresses(addresses, allow_loopback=local)
    headers: dict[str, str] = {}
    if credential_ref:
        headers["Authorization"] = f"Bearer {resolve_configured_secret(credential_ref, settings)}"
    timeout = httpx2.Timeout(settings.mcp_timeout_seconds)
    transport = _PinnedTransport(
        host=host,
        port=port,
        addresses=addresses,
        allow_loopback=local,
        max_bytes=settings.mcp_max_bytes,
        resolver=resolver,
        verify=_verify(settings),
        dispatch=dispatch,
    )
    async with httpx2.AsyncClient(
        transport=transport,
        timeout=timeout,
        follow_redirects=False,
        max_redirects=MCP_MAX_REDIRECTS,
        headers=headers,
        trust_env=False,
        limits=httpx2.Limits(max_connections=4, max_keepalive_connections=2),
    ) as http:
        if http.trust_env or http.max_redirects != MCP_MAX_REDIRECTS:
            raise DomainError("endpoint_rejected", "The MCP client boundary is not active.")
        async with Client(
            streamable_http_client(
                url,
                http_client=http,
                max_sse_event_size=settings.mcp_max_bytes,
            )
        ) as client:
            return await operation(client)


async def _list(client: Client) -> list[dict[str, Any]]:
    listed = await client.list_tools()
    described: list[dict[str, Any]] = []
    for tool in getattr(listed, "tools", []):
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
        described.append(
            {
                "name": str(tool.name),
                "description": str(getattr(tool, "description", "") or ""),
                "input_schema": schema if isinstance(schema, dict) else {},
            }
        )
    return described


def _structured(result: Any) -> dict[str, Any]:
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        raise DomainError("capability_failed", "The MCP tool reported an error.")
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if (
        isinstance(structured, dict)
        and set(structured) == {"result"}
        and isinstance(structured["result"], dict)
    ):
        return structured["result"]
    if isinstance(structured, dict):
        return structured
    raise DomainError("invalid_output", "The MCP tool did not return a structured object.")


def _request_body(request: httpx2.Request) -> bytes:
    try:
        return bytes(request.content)
    except Exception:
        return b""


def _verify(settings: Settings) -> ssl.SSLContext | bool:
    if settings.environment == "production":
        return True
    path = os.environ.get("APEX_MCP_CA_FILE", "")
    if not path:
        return True
    return ssl.create_default_context(cafile=path)


class _PinnedTransport(httpx2.AsyncBaseTransport):
    """Connect only to the addresses validated for this request, and reject a later change."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        addresses: list[str],
        allow_loopback: bool,
        max_bytes: int,
        resolver: Resolver | None,
        verify: ssl.SSLContext | bool,
        dispatch: _ToolDispatch | None = None,
        inner: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._addresses = list(addresses)
        self._allow_loopback = allow_loopback
        self._max_bytes = max_bytes
        self._resolver = resolver
        self._dispatch = dispatch or _ToolDispatch()
        self._inner = inner or httpx2.AsyncHTTPTransport(verify=verify, retries=0)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        requested = request.url.host
        if requested not in {self._host, *self._addresses}:
            raise DomainError("endpoint_rejected", "The endpoint redirected the request.")
        current = resolve_host(self._host, self._port, self._resolver)
        assert_safe_addresses(current, allow_loopback=self._allow_loopback)
        if set(current) != set(self._addresses):
            raise DomainError(
                "endpoint_rejected", "The endpoint address changed during the request."
            )
        body = _request_body(request)
        tool_call = request.method == "POST" and _TOOL_CALL in body
        pinned = httpx2.Request(
            request.method,
            request.url.copy_with(host=current[0]),
            headers=request.headers,
            content=body,
            extensions={**request.extensions, "sni_hostname": self._host},
        )
        try:
            response = await self._inner.handle_async_request(pinned)
        except (httpx2.ConnectTimeout, httpx2.ConnectError, httpx2.PoolTimeout):
            raise
        except Exception:
            if tool_call:
                self._dispatch.sent = True
            raise
        if tool_call:
            self._dispatch.sent = True
        await response.aread()
        if len(response.content) > self._max_bytes:
            raise DomainError("endpoint_rejected", "The endpoint response is too large.")
        media = response.headers.get("content-type", "")
        if media.startswith("text/html") or media.startswith("image/"):
            raise DomainError("endpoint_rejected", "The endpoint returned an unexpected response.")
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()
