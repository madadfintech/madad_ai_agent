"""Shared MCP client: dispatch, recording, retry, timeout, error mapping."""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import McpSettings
from app.shared.mcp import InMemoryMCPClient, MCPError, MCPToolCaller
from app.shared.mcp.client import HttpMCPClient


async def _nosleep(_: float) -> None:
    return None


def test_http_client_reuses_one_connection_pool():
    client = HttpMCPClient(McpSettings(endpoint="https://mcp.example"))
    assert client._http() is client._http()  # persistent, not per-call


async def test_http_client_aclose_is_idempotent_and_safe():
    client = HttpMCPClient(McpSettings(endpoint="https://mcp.example"))
    await client.aclose()  # nothing opened yet — must not raise
    client._http()
    await client.aclose()
    assert client._client is None


async def test_call_tool_dispatches_to_handler_and_records():
    client = InMemoryMCPClient(handlers={"t.echo": lambda p: {"echo": p}})
    result = await client.call_tool("t.echo", {"x": 1})
    assert result == {"echo": {"x": 1}}
    assert client.calls == [("t.echo", {"x": 1})]


async def test_unmapped_tool_returns_empty_dict():
    client = InMemoryMCPClient()
    assert await client.call_tool("t.unknown", {}) == {}


def test_in_memory_client_satisfies_protocol():
    assert isinstance(InMemoryMCPClient(), MCPToolCaller)


async def test_retry_succeeds_after_transient_failures():
    client = InMemoryMCPClient(
        handlers={"t.ok": lambda p: {"ok": True}},
        fail_times=2,
        settings=McpSettings(retry_max_attempts=3, retry_base_delay_seconds=0.0),
        sleep=_nosleep,
    )
    assert await client.call_tool("t.ok", {}) == {"ok": True}


async def test_retry_exhausted_raises_mcp_error_502():
    client = InMemoryMCPClient(
        fail_times=5,
        settings=McpSettings(retry_max_attempts=2, retry_base_delay_seconds=0.0),
        sleep=_nosleep,
    )
    with pytest.raises(MCPError) as exc:
        await client.call_tool("t.fail", {})
    assert exc.value.http_status == 502
    assert exc.value.details["tool"] == "t.fail"


async def test_no_retry_by_default():
    client = InMemoryMCPClient(fail_times=1)  # default retry_max_attempts=1
    with pytest.raises(MCPError):
        await client.call_tool("t.once", {})
    assert len(client.calls) == 1  # tried exactly once


async def test_timeout_maps_to_mcp_error():
    class SlowClient(InMemoryMCPClient):
        async def _invoke(self, name, payload):
            await asyncio.sleep(1.0)
            return {}

    client = SlowClient(settings=McpSettings(timeout_seconds=0.01))
    with pytest.raises(MCPError):
        await client.call_tool("t.slow", {})
