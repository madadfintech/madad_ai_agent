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
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Generator
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

from app.core.config import McpSettings
from app.core.exceptions import UpstreamError
from app.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    import fastmcp

SleepFn = Callable[[float], Awaitable[None]]
HandlerFn = Callable[[dict[str, Any]], dict[str, Any]]
IamTokenFetcher = Callable[[str], str]
# google-auth's id_token.fetch_id_token is a (audience: str) -> str function
# under the hood. We pass it through this alias so tests can inject a stub.

# Refresh ID tokens this many seconds BEFORE expiry to keep calls smooth.
_ID_TOKEN_LIFETIME_SECONDS = 3300  # google-issued ID tokens are valid for 1h
_ID_TOKEN_REFRESH_LEEWAY_SECONDS = 60


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
                raw = await asyncio.wait_for(
                    self._invoke(name, payload), timeout=self._settings.timeout_seconds
                )
                return _unwrap_madad_envelope(raw)
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


def _default_iam_token_fetcher(audience: str) -> str:
    """Mint a Google-signed ID token for the given Cloud Run audience.

    Uses Application Default Credentials. On Cloud Run this is the runtime
    service account; locally use ``gcloud auth application-default login``.
    Lazy-imported so the module stays importable without ``google-auth``.
    """

    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    return id_token.fetch_id_token(Request(), audience)  # type: ignore[no-untyped-call,no-any-return]


class IamIdTokenAuth(httpx.Auth):
    """``httpx.Auth`` that injects a fresh Cloud Run ID token on every request.

    Tokens are cached and refreshed about a minute before expiry so a long-
    lived MCP client doesn't drop traffic when Google rotates tokens. The
    ``fetcher`` argument lets tests inject a deterministic stub.
    """

    def __init__(
        self,
        audience: str,
        *,
        fetcher: IamTokenFetcher = _default_iam_token_fetcher,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not audience:
            raise ValueError("IamIdTokenAuth requires a non-empty audience")
        self._audience = audience
        self._fetcher = fetcher
        self._clock = clock
        self._token: str | None = None
        self._expires_at: float = 0.0

    def _token_is_fresh(self, now: float) -> bool:
        return (
            self._token is not None
            and now < self._expires_at - _ID_TOKEN_REFRESH_LEEWAY_SECONDS
        )

    def _refresh(self) -> str:
        now = self._clock()
        self._token = self._fetcher(self._audience)
        self._expires_at = now + _ID_TOKEN_LIFETIME_SECONDS
        return self._token

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        token = self._token if self._token_is_fresh(self._clock()) else self._refresh()
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


# Factory hook so subclasses (and tests) can override how the underlying
# fastmcp.Client is constructed.
def _build_fastmcp_client(
    settings: McpSettings,
    *,
    iam_token_fetcher: IamTokenFetcher = _default_iam_token_fetcher,
) -> fastmcp.Client[Any]:
    import fastmcp

    auth: Any = None
    if settings.auth_mode == "bearer":
        if settings.auth_token:
            auth = settings.auth_token  # fastmcp accepts a raw bearer string
    elif settings.auth_mode == "iam":
        if not settings.iam_audience:
            raise MCPError(
                "MCP auth_mode='iam' requires settings.iam_audience to be set",
                details={"endpoint": settings.endpoint},
            )
        auth = IamIdTokenAuth(settings.iam_audience, fetcher=iam_token_fetcher)
    else:
        raise MCPError(
            f"Unknown MCP auth_mode {settings.auth_mode!r}",
            details={"auth_mode": settings.auth_mode},
        )
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

    # QA #1 (2026-06-09): markers fastmcp raises when its context manager
    # has exited / the underlying transport dropped. When seen, drop the
    # stale client + stack and reconnect for one retry — the offending
    # call didn't reach the server, so retrying is safe even for writes.
    _DISCONNECT_MARKERS = (
        "client is not connected",
        "use the 'async with client:' context manager",
        "connection closed",
        "connection reset",
        "stream closed",
        "transport closed",
        "broken pipe",
        "remote disconnected",
    )

    async def _ensure_connected(self) -> fastmcp.Client[Any]:
        if self._client is None:
            self._client = await self._stack.enter_async_context(
                self._client_factory(self._settings)
            )
        assert self._client is not None  # narrowing for mypy
        return self._client

    async def _reset_connection(self) -> None:
        """Drop the current client + stack so the next call mints fresh.

        Used by the reconnect path after a disconnect — the stale stack
        may itself raise on close (broken transport), so swallow that to
        avoid masking the original disconnect error."""

        try:
            await self._stack.aclose()
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            self._log.warning("mcp.reconnect.stack_close_failed", error=str(exc)[:200])
        self._stack = AsyncExitStack()
        self._client = None

    @classmethod
    def _looks_like_disconnect(cls, exc: BaseException) -> bool:
        msg = str(exc).lower()
        return any(marker in msg for marker in cls._DISCONNECT_MARKERS)

    async def _invoke(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._ensure_connected()
        try:
            result = await client.call_tool(name, payload)
        except Exception as exc:
            if not self._looks_like_disconnect(exc):
                raise
            # QA #1 blocker: the persistent client's transport dropped and
            # every subsequent call would re-raise "Client is not
            # connected" until the container restarts. Reconnect once and
            # replay the call — the request never reached the server.
            self._log.warning(
                "mcp.reconnect.attempt", tool=name, error=str(exc)[:200],
            )
            await self._reset_connection()
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


def _unwrap_madad_envelope(resp: Any) -> dict[str, Any]:
    """Strip Madad's ``{status_code, body, set_cookie_present?}`` envelope.

    Every Madad MCP tool wraps its REST-style payload in this shape, e.g.::

        {"status_code": 200,
         "body": {"exists": true, "field": "email", ...},
         "set_cookie_present": false}

    The agent's adapters work in terms of the inner body. This helper
    returns the body when the envelope is present and the response as-is
    otherwise — so :class:`InMemoryMCPClient` handlers (which return flat
    test shapes) keep working untouched.
    """

    if (
        isinstance(resp, dict)
        and "status_code" in resp
        and isinstance(resp.get("body"), dict)
    ):
        body: dict[str, Any] = resp["body"]
        return body
    return resp if isinstance(resp, dict) else {}
