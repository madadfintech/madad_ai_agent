"""Shared MCP client — the single contract every service dispatches through.

The platform never calls Madad/Tess/WhatsApp/Email directly; it calls MCP tools
on the cluster via Streamable HTTP. This module defines:

* :class:`MCPToolCaller` — the structural protocol adapters depend on.
* :class:`MCPClient` — base class adding timeout + idempotency-gated retry +
  error normalisation around a subclass ``_invoke``.
* :class:`InMemoryMCPClient` — recording fake driven by per-tool handlers (tests).
* :class:`HttpMCPClient` — production client. Wraps :class:`fastmcp.Client`
  over Streamable HTTP. Authentication is configurable via ``McpSettings``
  (Phase 0 ships the bearer mode; Cloud Run IAM lands in a follow-up).

Retry semantics: writes are single-shot by default. A tool participates in
retry only if it appears in ``settings.idempotent_tools`` (typically populated
with ``Tools.read_only()`` plus ``Tools.payment_idempotent_writes()``).
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from app.core.config import McpSettings
from app.core.exceptions import UpstreamError
from app.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    import fastmcp

SleepFn = Callable[[float], Awaitable[None]]
HandlerFn = Callable[[dict[str, Any]], dict[str, Any]]


class MCPError(UpstreamError):
    """An MCP tool call failed (transport, timeout, or tool-side error)."""

    code = "mcp_error"


@runtime_checkable
class MCPToolCaller(Protocol):
    """Structural interface the service gateways/adapters depend on."""

    async def call_tool(self, name: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class MCPClient(ABC):
    """Base client: timeout + idempotency-gated retry + error normalisation."""

    def __init__(self, settings: McpSettings, *, sleep: SleepFn | None = None) -> None:
        self._settings = settings
        self._sleep: SleepFn = sleep or asyncio.sleep
        self._log = get_logger("mcp.client")

    def is_idempotent(self, name: str) -> bool:
        """A tool call is safe to retry only if explicitly registered as
        idempotent (read-only or backend-honoured idempotency-key writes)."""

        return name in self._settings.idempotent_tools

    async def call_tool(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Writes that are NOT idempotency-key-honoured run single-shot regardless
        # of the configured retry budget — protects against duplicate uploads,
        # duplicate payment links, etc. (Q10 — backend support is still partial).
        requested = max(1, self._settings.retry_max_attempts)
        attempts = requested if self.is_idempotent(name) else 1
        delay = self._settings.retry_base_delay_seconds
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await asyncio.wait_for(
                    self._invoke(name, payload), timeout=self._settings.timeout_seconds
                )
            except Exception as exc:  # noqa: BLE001 - normalise + (optionally) retry
                last_error = exc
                self._log.warning(
                    "mcp.call_failed",
                    tool=name,
                    attempt=attempt,
                    attempts=attempts,
                    error=str(exc),
                )
                if attempt >= attempts:
                    break
                await self._sleep(delay)
                delay = min(delay * 2, self._settings.retry_max_delay_seconds)
        raise MCPError(
            f"MCP tool {name!r} failed after {attempts} attempt(s)",
            details={"tool": name, "attempts": attempts},
        ) from last_error

    @abstractmethod
    async def _invoke(self, name: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        """Release any held resources (overridden by clients with connections)."""

        return None


class InMemoryMCPClient(MCPClient):
    """Recording fake. ``handlers`` maps a tool name to a response builder;
    unmapped tools return ``{}``. ``fail_times`` makes the first N invocations
    raise, to exercise the base client's retry behaviour."""

    def __init__(
        self,
        *,
        handlers: dict[str, HandlerFn] | None = None,
        fail_times: int = 0,
        settings: McpSettings | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        super().__init__(settings or McpSettings(), sleep=sleep)
        self._handlers = handlers or {}
        self._fail_times = fail_times
        self._invocations = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _invoke(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._invocations += 1
        self.calls.append((name, payload))
        if self._invocations <= self._fail_times:
            raise RuntimeError(f"simulated MCP transport failure #{self._invocations}")
        handler = self._handlers.get(name)
        return handler(payload) if handler is not None else {}


# Factory hook so subclasses (and tests) can override how the underlying
# fastmcp.Client is constructed. Default uses bearer auth from settings.
def _build_fastmcp_client(settings: McpSettings) -> fastmcp.Client[Any]:
    import fastmcp

    auth: Any = None
    if settings.auth_mode == "bearer" and settings.auth_token:
        auth = settings.auth_token  # fastmcp.Client accepts a bearer token string
    # IAM mode is wired in a follow-up commit (Cloud Run ID-token via google-auth).
    return fastmcp.Client(settings.endpoint, auth=auth, timeout=settings.timeout_seconds)


class HttpMCPClient(MCPClient):
    """Production MCP client wrapping :class:`fastmcp.Client`.

    Holds ONE persistent ``fastmcp.Client`` whose connection lifecycle is
    managed by an ``AsyncExitStack`` (entered lazily on first call, closed
    by :meth:`aclose`). This pools transport state across tool calls instead
    of paying the connect cost per call.
    """

    def __init__(
        self,
        settings: McpSettings,
        *,
        sleep: SleepFn | None = None,
        client_factory: Callable[[McpSettings], fastmcp.Client[Any]] | None = None,
    ) -> None:
        super().__init__(settings, sleep=sleep)
        self._client_factory = client_factory or _build_fastmcp_client
        self._stack = AsyncExitStack()
        self._client: fastmcp.Client[Any] | None = None

    async def _ensure_connected(self) -> fastmcp.Client[Any]:
        if self._client is None:
            self._client = await self._stack.enter_async_context(
                self._client_factory(self._settings)
            )
        assert self._client is not None  # narrowing for mypy
        return self._client

    async def _invoke(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._ensure_connected()
        result = await client.call_tool(name, payload)
        if getattr(result, "is_error", False):
            raise MCPError(f"MCP tool {name!r} returned is_error", details={"tool": name})
        data = getattr(result, "data", None)
        if isinstance(data, dict):
            return data
        # Tools that return a scalar / list / None — wrap so the contract stays
        # ``dict[str, Any]``. Adapters that need the raw shape can read ``result``.
        return {"result": data}

    async def aclose(self) -> None:
        await self._stack.aclose()
        self._client = None
