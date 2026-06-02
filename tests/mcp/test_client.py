"""Shared MCP client: dispatch, idempotency-gated retry, error mapping, fastmcp wrapper."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.config import McpSettings
from app.shared.mcp import InMemoryMCPClient, MCPError, MCPToolCaller
from app.shared.mcp.client import HttpMCPClient


async def _nosleep(_: float) -> None:
    return None


# -- in-memory dispatcher contract --------------------------------------------


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


# -- idempotency-gated retry --------------------------------------------------


async def test_non_idempotent_tool_is_single_shot_regardless_of_retry_budget():
    # retry_max_attempts is 3 but the tool is NOT in idempotent_tools → single-shot.
    client = InMemoryMCPClient(
        handlers={"t.write": lambda p: {"ok": True}},
        fail_times=1,
        settings=McpSettings(retry_max_attempts=3, retry_base_delay_seconds=0.0),
        sleep=_nosleep,
    )
    with pytest.raises(MCPError):
        await client.call_tool("t.write", {})
    assert client._invocations == 1  # exactly one attempt — no retry


async def test_idempotent_tool_retries_up_to_budget_and_succeeds():
    client = InMemoryMCPClient(
        handlers={"t.read": lambda p: {"ok": True}},
        fail_times=2,
        settings=McpSettings(
            retry_max_attempts=3,
            retry_base_delay_seconds=0.0,
            idempotent_tools={"t.read"},
        ),
        sleep=_nosleep,
    )
    assert await client.call_tool("t.read", {}) == {"ok": True}
    assert client._invocations == 3  # failed twice, succeeded third


async def test_idempotent_tool_retry_exhausted_raises_mcp_error_with_attempts():
    client = InMemoryMCPClient(
        fail_times=5,
        settings=McpSettings(
            retry_max_attempts=2,
            retry_base_delay_seconds=0.0,
            idempotent_tools={"t.flaky"},
        ),
        sleep=_nosleep,
    )
    with pytest.raises(MCPError) as exc:
        await client.call_tool("t.flaky", {})
    assert exc.value.http_status == 502
    assert exc.value.details["tool"] == "t.flaky"
    assert exc.value.details["attempts"] == 2


async def test_is_idempotent_helper_reads_settings_set():
    client = InMemoryMCPClient(
        settings=McpSettings(idempotent_tools={"madad_auth_me", "madad_kyc_get_documents"})
    )
    assert client.is_idempotent("madad_auth_me") is True
    assert client.is_idempotent("madad_kyc_upload_document_base64") is False


async def test_timeout_maps_to_mcp_error():
    class SlowClient(InMemoryMCPClient):
        async def _invoke(self, name, payload):
            await asyncio.sleep(1.0)
            return {}

    client = SlowClient(settings=McpSettings(timeout_seconds=0.01))
    with pytest.raises(MCPError):
        await client.call_tool("t.slow", {})


# -- HttpMCPClient: fastmcp.Client wrapper -----------------------------------


class _FakeCallResult:
    def __init__(self, data: Any, is_error: bool = False) -> None:
        self.data = data
        self.is_error = is_error


class _FakeFastMcpClient:
    """Minimal stand-in for fastmcp.Client used to exercise the wrapper without
    a running MCP server."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self._responses = responses or {}
        self.entered = 0
        self.exited = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> _FakeFastMcpClient:
        self.entered += 1
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.exited += 1

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _FakeCallResult:
        self.calls.append((name, arguments))
        response = self._responses.get(name, {})
        if isinstance(response, Exception):
            raise response
        return _FakeCallResult(response)


async def test_http_client_enters_fastmcp_session_lazily_and_reuses_it():
    fake = _FakeFastMcpClient(responses={"madad_auth_me": {"id": "u1"}})
    client = HttpMCPClient(
        McpSettings(endpoint="https://mcp.example", idempotent_tools={"madad_auth_me"}),
        client_factory=lambda _s: fake,  # type: ignore[arg-type, return-value]
    )

    assert fake.entered == 0  # construction does not connect
    result = await client.call_tool("madad_auth_me", {"access_token": "tok"})
    assert result == {"id": "u1"}
    assert fake.entered == 1

    await client.call_tool("madad_auth_me", {"access_token": "tok"})
    assert fake.entered == 1  # session reused, not re-opened

    await client.aclose()
    assert fake.exited == 1


async def test_http_client_passes_arguments_and_unwraps_call_tool_result():
    fake = _FakeFastMcpClient(responses={"madad_kyc_get_business_details": {"id": "b1"}})
    client = HttpMCPClient(
        McpSettings(endpoint="https://mcp.example"),
        client_factory=lambda _s: fake,  # type: ignore[arg-type, return-value]
    )
    out = await client.call_tool(
        "madad_kyc_get_business_details", {"access_token": "tok"}
    )
    assert out == {"id": "b1"}
    assert fake.calls == [("madad_kyc_get_business_details", {"access_token": "tok"})]
    await client.aclose()


async def test_http_client_wraps_non_dict_response_under_result_key():
    fake = _FakeFastMcpClient(responses={"t.list": [1, 2, 3]})
    client = HttpMCPClient(
        McpSettings(endpoint="https://mcp.example"),
        client_factory=lambda _s: fake,  # type: ignore[arg-type, return-value]
    )
    out = await client.call_tool("t.list", {})
    assert out == {"result": [1, 2, 3]}
    await client.aclose()


async def test_http_client_is_error_raises_mcp_error():
    fake = _FakeFastMcpClient()
    fake._responses["t.broken"] = {}

    async def fake_call_tool(name: str, arguments: dict[str, Any]) -> _FakeCallResult:
        return _FakeCallResult({}, is_error=True)

    fake.call_tool = fake_call_tool  # type: ignore[method-assign]

    client = HttpMCPClient(
        McpSettings(endpoint="https://mcp.example"),
        client_factory=lambda _s: fake,  # type: ignore[arg-type, return-value]
    )
    with pytest.raises(MCPError):
        await client.call_tool("t.broken", {})
    await client.aclose()


async def test_http_client_aclose_is_safe_when_never_connected():
    client = HttpMCPClient(
        McpSettings(endpoint="https://mcp.example"),
        client_factory=lambda _s: _FakeFastMcpClient(),  # type: ignore[arg-type, return-value]
    )
    await client.aclose()  # never connected — must not raise
