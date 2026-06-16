"""Shared MCP client + tool registry.

The single integration point for the MCP cluster: one ``MCPToolCaller`` contract,
a client with timeout/retry/error-normalisation, a recording fake for tests, and
a central provisional tool-name registry. Services select the real client via
``settings.mcp.enabled``; the on-the-wire protocol + endpoint/auth are the only
catalog-blocked seams.
"""

from __future__ import annotations

from .client import (
    HttpMCPClient,
    InMemoryMCPClient,
    MCPAuthError,
    MCPBackendError,
    MCPClient,
    MCPConflictError,
    MCPError,
    MCPNotFoundError,
    MCPTimeoutError,
    MCPToolCaller,
)
from .provider import get_mcp_client
from .registry import Tools

__all__ = [
    "HttpMCPClient",
    "InMemoryMCPClient",
    "MCPAuthError",
    "MCPBackendError",
    "MCPClient",
    "MCPConflictError",
    "MCPError",
    "MCPNotFoundError",
    "MCPTimeoutError",
    "MCPToolCaller",
    "Tools",
    "get_mcp_client",
]
