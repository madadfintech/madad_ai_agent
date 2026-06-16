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
from typing import Any

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


def _default_tool_timeouts() -> dict[str, float]:
    """Per-tool timeout overrides (seconds).

    Defaults cover the tools that routinely take much longer than the
    global timeout. UAT 2026-06-16 (+918287611995): the SME's WhatsApp
    invoice uploads were all timing out at 30s — backend extract+submit
    takes 60-90s on real-world invoices (OCR + extraction + submission
    in one round-trip) so the agent gave up and sent the SME a
    misleading "couldn't read the file" message. 120s comfortably covers
    the observed tail; further increases just delay genuinely-broken
    calls without helping the live case.
    """

    return {
        Tools.INVOICES_EXTRACT_AND_SUBMIT_INVOICE_BASE64: 120.0,
        Tools.INVOICES_EXTRACT_AND_SUBMIT_INVOICE: 120.0,
        Tools.INVOICES_UPLOAD_ZIP: 180.0,  # ZIPs can carry many invoices
        # KYC classify path is also OCR-heavy; the docs loop already wraps
        # it in its own 50s wait_for but the underlying MCP layer should
        # not pre-empt that with a 30s cap.
        Tools.KYC_CLASSIFY_AND_UPLOAD_DOCUMENT_BASE64: 90.0,
        Tools.KYC_CLASSIFY_AND_UPLOAD_ZIP_BASE64: 180.0,
    }


@lru_cache(maxsize=1)
def get_mcp_client() -> MCPClient:
    from app.core.config import settings

    s = settings.mcp
    # Apply the project defaults if the operator has not customised the sets.
    updates: dict[str, Any] = {}
    if not s.idempotent_tools:
        updates["idempotent_tools"] = _default_idempotent_tools()
    if not s.tool_timeouts:
        updates["tool_timeouts"] = _default_tool_timeouts()
    if updates:
        s = s.model_copy(update=updates)
    return HttpMCPClient(s)
