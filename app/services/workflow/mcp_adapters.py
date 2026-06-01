"""MCP-backed onboarding adapters (CATALOG-DEPENDENT scaffolds).

Bridges the onboarding ports (MadadClient, PaymentClient, DocumentIntake) to the
shared MCP client. Tool names come from ``app.shared.mcp.registry.Tools`` and the
payload/response shapes are PROVISIONAL — they encode our best understanding of
the Madad/Tess contracts and are tested against a fake MCP caller. When Ishan's
catalog lands, reconcile the registry names + the field names below; no other
code needs to change (selection is via ``settings.mcp.enabled``).
"""

from __future__ import annotations

from typing import Any

from app.shared.mcp import MCPToolCaller, Tools

from .ports import DocumentIntake, MadadClient, PaymentClient


class McpMadadClient(MadadClient):
    """Madad financing decisions via MCP tools."""

    def __init__(self, tool_caller: MCPToolCaller) -> None:
        self._tools = tool_caller

    async def check_eligibility(self, cr_ref: str | None) -> bool:
        result = await self._tools.call_tool(Tools.ELIGIBILITY_CHECK, {"cr_ref": cr_ref})
        return bool(result.get("eligible", False))

    async def request_prequalification(self, *, identity: str, cr_ref: str | None) -> str:
        result = await self._tools.call_tool(
            Tools.PREQUAL_REQUEST, {"identity": identity, "cr_ref": cr_ref}
        )
        return str(result["application_ref"])

    async def request_score(self, application_ref: str | None) -> None:
        await self._tools.call_tool(Tools.SCORE_REQUEST, {"application_ref": application_ref})

    async def submit_to_lenders(self, application_ref: str | None) -> None:
        await self._tools.call_tool(Tools.LENDERS_SUBMIT, {"application_ref": application_ref})

    async def activate_credit_line(
        self, application_ref: str | None, offer: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self._tools.call_tool(
            Tools.CREDITLINE_ACTIVATE, {"application_ref": application_ref, "offer": offer}
        )
        return dict(result)


class McpPaymentClient(PaymentClient):
    """Tess payment-link creation via an MCP tool."""

    def __init__(self, tool_caller: MCPToolCaller) -> None:
        self._tools = tool_caller

    async def create_link(self, *, application_ref: str | None, amount: int) -> str:
        result = await self._tools.call_tool(
            Tools.PAYMENT_CREATE_LINK, {"application_ref": application_ref, "amount": amount}
        )
        return str(result["link"])


class McpDocumentIntake(DocumentIntake):
    """Document routing + checklist via MCP (Madad extraction microservice).

    Channel documents arrive as a ``provider_ref`` that Madad fetches; the
    platform stores no bytes (data sovereignty).
    """

    def __init__(self, tool_caller: MCPToolCaller) -> None:
        self._tools = tool_caller

    async def ingest(
        self, *, application_ref: str | None, filename: str, provider_ref: str | None = None
    ) -> None:
        await self._tools.call_tool(
            Tools.DOCUMENT_PROCESS,
            {
                "application_ref": application_ref,
                "filename": filename,
                "provider_ref": provider_ref,
            },
        )

    async def missing(self, *, checklist: str, application_ref: str | None) -> list[str]:
        result = await self._tools.call_tool(
            Tools.DOCUMENT_CHECKLIST,
            {"checklist": checklist, "application_ref": application_ref},
        )
        return [str(code) for code in result.get("missing", [])]
