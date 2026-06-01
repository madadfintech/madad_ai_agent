"""Shared MCP client — the single contract every service dispatches through.

The platform never calls Madad/Tess/WhatsApp/Email directly; it calls MCP tools
on the cluster (owned by a separate team). This module defines:

* ``MCPToolCaller`` — the structural protocol adapters depend on (one definition,
  was previously duplicated per service).
* ``MCPClient`` — an abstract base adding timeout + optional transport retry +
  error normalisation around a subclass ``_invoke``.
* ``InMemoryMCPClient`` — a recording fake (tests/dev) driven by per-tool handlers.
* ``HttpMCPClient`` — the real client. The on-the-wire protocol (path/envelope)
  is PROVISIONAL and is the only catalog-blocked seam; everything else is ready.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from app.core.config import McpSettings
from app.core.exceptions import UpstreamError
from app.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    import httpx

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
    """Base client: timeout + optional retry + error normalisation."""

    def __init__(self, settings: McpSettings, *, sleep: SleepFn | None = None) -> None:
        self._settings = settings
        self._sleep: SleepFn = sleep or asyncio.sleep
        self._log = get_logger("mcp.client")

    async def call_tool(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        attempts = max(1, self._settings.retry_max_attempts)
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
                    "mcp.call_failed", tool=name, attempt=attempt, error=str(exc)
                )
                if attempt >= attempts:
                    break
                await self._sleep(delay)
                delay = min(delay * 2, self._settings.retry_max_delay_seconds)
        raise MCPError(
            f"MCP tool {name!r} failed after {attempts} attempt(s)",
            details={"tool": name},
        ) from last_error

    @abstractmethod
    async def _invoke(self, name: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        """Release any held resources (overridden by clients with connections)."""

        return None


class InMemoryMCPClient(MCPClient):
    """Recording fake. ``handlers`` maps a tool name to a response builder;
    unmapped tools return ``{}``. ``fail_times`` makes the first N invocations
    raise, to exercise the base client's retry."""

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


class HttpMCPClient(MCPClient):
    """Real MCP client over HTTPS.

    Holds ONE persistent ``httpx.AsyncClient`` so connections (and TLS) are
    pooled and reused across tool calls instead of dialled per call.

    CATALOG-DEPENDENT: the request path/envelope below is a placeholder for the
    MCP cluster's actual contract — confirm and adjust when the catalog lands.
    ``httpx`` is imported lazily so this module stays importable without it.
    """

    def __init__(self, settings: McpSettings, *, sleep: SleepFn | None = None) -> None:
        super().__init__(settings, sleep=sleep)
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        import httpx

        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self._settings.auth_token:
                headers["Authorization"] = f"Bearer {self._settings.auth_token}"
            self._client = httpx.AsyncClient(
                base_url=self._settings.endpoint.rstrip("/"),
                timeout=self._settings.timeout_seconds,
                headers=headers,
            )
        return self._client

    async def _invoke(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        # PROVISIONAL path/envelope — reconcile with the MCP cluster contract.
        response = await self._http().post(
            f"/tools/{name}/call", json={"tool": name, "arguments": payload}
        )
        response.raise_for_status()
        data: Any = response.json()
        return data if isinstance(data, dict) else {"result": data}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
