"""Process-singleton MCP client built from settings.

Only used when ``settings.mcp.enabled`` is true (production); dev/tests keep
their in-memory gateways and never construct this.

The provider ensures the production client knows which tools are safe to
retry. By default it registers ``Tools.read_only() | Tools.payment_idempotent_writes()``
unless the operator has supplied an explicit ``idempotent_tools`` set in
settings (e.g. to whitelist specific KYC writes once Ishan's backend honours
``Idempotency-Key`` on those).
"""

from __future__ import annotations

from functools import lru_cache

from .client import HttpMCPClient, MCPClient
from .registry import Tools


def _default_idempotent_tools() -> set[str]:
    """Tools the production client may retry transparently.

    * Every read tool (``Tools.read_only()``) — safe by definition.
    * The two payment write tools that accept ``idempotency_key``
      (``Tools.payment_idempotent_writes()``) — backend-honoured, safe to retry
      when the same key is reused.
    """

    return Tools.read_only() | Tools.payment_idempotent_writes()


@lru_cache(maxsize=1)
def get_mcp_client() -> MCPClient:
    from app.core.config import settings

    s = settings.mcp
    # Apply the project default if the operator has not customised the set.
    if not s.idempotent_tools:
        s = s.model_copy(update={"idempotent_tools": _default_idempotent_tools()})
    return HttpMCPClient(s)
