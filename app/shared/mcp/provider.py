"""Process-singleton MCP client built from settings.

Only used when ``settings.mcp.enabled`` is true (production); dev/tests keep
their in-memory gateways and never construct this.
"""

from __future__ import annotations

from functools import lru_cache

from .client import HttpMCPClient, MCPClient


@lru_cache(maxsize=1)
def get_mcp_client() -> MCPClient:
    from app.core.config import settings

    return HttpMCPClient(settings.mcp)
