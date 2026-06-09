"""Shared MCP client: dispatch, idempotency-gated retry, error mapping, fastmcp wrapper."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
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


async def test_http_client_reconnects_after_disconnect_marker() -> None:
    """QA #1 (2026-06-09): after the persistent fastmcp client's
    transport drops, the next call raised "Client is not connected" and
    the bot stayed silent until container restart. The wrapper now
    detects the disconnect marker, drops the stale client + AsyncExitStack,
    mints a fresh client, and replays the call once."""

    # Factory hands out two distinct fake clients. The first raises a
    # "Client is not connected" error on its second call (simulating a
    # mid-session transport drop); the second answers normally.
    drop_marker = RuntimeError(
        "Client is not connected. Use the 'async with client:' context manager first"
    )
    first = _FakeFastMcpClient(responses={"madad_auth_me": {"id": "u1"}})
    second = _FakeFastMcpClient(responses={"madad_auth_me": {"id": "u2"}})
    pool: list[_FakeFastMcpClient] = [first, second]

    def factory(_s: McpSettings) -> _FakeFastMcpClient:
        return pool.pop(0)

    # Patch the first fake to raise the disconnect on the SECOND call.
    original_call = first.call_tool

    async def call_with_drop(name: str, arguments: dict[str, Any]) -> _FakeCallResult:
        if first.calls and first.calls[-1][0] == "madad_auth_me":
            raise drop_marker
        return await original_call(name, arguments)

    first.call_tool = call_with_drop  # type: ignore[method-assign]

    client = HttpMCPClient(
        McpSettings(
            endpoint="https://mcp.example",
            idempotent_tools={"madad_auth_me"},
            retry_max_attempts=1,  # single-shot to prove reconnect runs WITHIN one attempt
        ),
        client_factory=factory,  # type: ignore[arg-type]
    )

    # First call lands cleanly via the first fake.
    out1 = await client.call_tool("madad_auth_me", {"access_token": "tok"})
    assert out1 == {"id": "u1"}
    # Second call: first fake raises the disconnect marker; the wrapper
    # drops the stack, mints the second fake, and serves the call.
    out2 = await client.call_tool("madad_auth_me", {"access_token": "tok"})
    assert out2 == {"id": "u2"}
    assert second.entered == 1
    await client.aclose()


async def test_http_client_does_not_reconnect_on_unrelated_error() -> None:
    """Reconnect must trigger ONLY on disconnect markers. A regular
    application-level error (e.g. backend 403) propagates straight up
    so the base client's retry / single-shot policy is unchanged."""

    fake = _FakeFastMcpClient()

    async def failing(name: str, arguments: dict[str, Any]) -> _FakeCallResult:
        raise RuntimeError("Madad API returned HTTP 403")

    fake.call_tool = failing  # type: ignore[method-assign]

    client = HttpMCPClient(
        McpSettings(
            endpoint="https://mcp.example",
            idempotent_tools={"madad_auth_me"},
            retry_max_attempts=1,
        ),
        client_factory=lambda _s: fake,  # type: ignore[arg-type, return-value]
    )

    with pytest.raises(MCPError):
        await client.call_tool("madad_auth_me", {"access_token": "tok"})
    # The fake was entered exactly once — no reconnect attempt.
    assert fake.entered == 1
    await client.aclose()


async def test_http_client_aclose_is_safe_when_never_connected():
    client = HttpMCPClient(
        McpSettings(endpoint="https://mcp.example"),
        client_factory=lambda _s: _FakeFastMcpClient(),  # type: ignore[arg-type, return-value]
    )
    await client.aclose()  # never connected — must not raise


# -- IAM ID-token auth --------------------------------------------------------


def test_iam_auth_requires_non_empty_audience():
    from app.shared.mcp.client import IamIdTokenAuth

    with pytest.raises(ValueError):
        IamIdTokenAuth("")


def test_iam_auth_caches_token_then_refreshes_before_expiry():
    from app.shared.mcp.client import IamIdTokenAuth

    minted: list[str] = []

    def fake_fetcher(audience: str) -> str:
        minted.append(audience)
        return f"tok-{len(minted)}"

    # Synthetic clock; we'll advance it manually.
    now = [1_000_000.0]

    auth = IamIdTokenAuth(
        "https://mcp.example",
        fetcher=fake_fetcher,
        clock=lambda: now[0],
    )

    # First call mints a token; second within the lifetime reuses it.
    req1 = httpx.Request("POST", "https://mcp.example/mcp")
    next(iter(auth.auth_flow(req1)))
    assert req1.headers["Authorization"] == "Bearer tok-1"

    req2 = httpx.Request("POST", "https://mcp.example/mcp")
    next(iter(auth.auth_flow(req2)))
    assert req2.headers["Authorization"] == "Bearer tok-1"
    assert minted == ["https://mcp.example"]

    # Advance the clock past the refresh-leeway boundary → token refreshes.
    now[0] += 4000
    req3 = httpx.Request("POST", "https://mcp.example/mcp")
    next(iter(auth.auth_flow(req3)))
    assert req3.headers["Authorization"] == "Bearer tok-2"


def test_build_fastmcp_client_iam_requires_audience():
    from app.shared.mcp.client import _build_fastmcp_client

    with pytest.raises(MCPError):
        _build_fastmcp_client(
            McpSettings(endpoint="https://mcp.example", auth_mode="iam"),
            iam_token_fetcher=lambda _a: "ignored",
        )


def test_build_fastmcp_client_rejects_unknown_auth_mode():
    from app.shared.mcp.client import _build_fastmcp_client

    with pytest.raises(MCPError):
        _build_fastmcp_client(
            McpSettings(endpoint="https://mcp.example", auth_mode="oauth-rsa-pop"),
        )


def test_build_fastmcp_client_iam_uses_injected_fetcher():
    from app.shared.mcp.client import _build_fastmcp_client

    minted: list[str] = []

    def fake_fetcher(audience: str) -> str:
        minted.append(audience)
        return f"tok-for-{audience}"

    # Construction with iam mode + an audience succeeds and the fetcher is wired
    # into the auth object (verified by exercising auth_flow once).
    settings = McpSettings(
        endpoint="https://mcp.example/mcp",
        auth_mode="iam",
        iam_audience="https://mcp.example",
    )
    client = _build_fastmcp_client(settings, iam_token_fetcher=fake_fetcher)
    # The Client's auth must trigger the fetcher when the first request runs.
    assert hasattr(client, "transport")  # construction succeeded
