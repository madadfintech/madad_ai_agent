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
        # UAT 2026-06-17 (+919497191690): SME's extract calls failed with
        # "We couldn't read the file" after ~90s. The web portal handles
        # the SAME invoice fine — confirming OCR is slow but works given
        # enough time. Bump extract budget to 180s so the agent's call
        # window matches what the OCR backend actually needs.
        Tools.INVOICES_EXTRACT_AND_SUBMIT_INVOICE_BASE64: 180.0,
        Tools.INVOICES_EXTRACT_AND_SUBMIT_INVOICE: 180.0,
        Tools.INVOICES_UPLOAD_ZIP: 240.0,  # ZIPs can carry many invoices
        Tools.INVOICES_EXTRACT_INVOICE_BASE64: 180.0,
        Tools.INVOICES_EXTRACT_INVOICE: 180.0,
        Tools.INVOICES_SUBMIT_INVOICE_BASE64: 120.0,
        Tools.INVOICES_SUBMIT_INVOICE: 120.0,
        # KYC classify path is also OCR-heavy. P0-1 (2026-07-07): the
        # classifier/extractor Cloud Run services now run scale-to-zero, so
        # the first call after idle pays a 1-3 minute cold start — the old
        # 90s/180s budgets (and the 30s global cap on the classify-only
        # tool) turned every first-after-idle batch into a false "upload
        # failed" and lost the SME's documents. Budgets now match the
        # invoice path (>=180s); the docs loop's outer wait_for is
        # _DOC_CLASSIFY_UPLOAD_TIMEOUT_SECONDS (default 240s) in
        # onboarding.py. Env-overridable via MCP__TOOL_TIMEOUTS.
        Tools.KYC_CLASSIFY_DOCUMENT_BASE64: 180.0,
        Tools.KYC_CLASSIFY_AND_UPLOAD_DOCUMENT_BASE64: 240.0,
        Tools.KYC_CLASSIFY_AND_UPLOAD_ZIP_BASE64: 240.0,
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
