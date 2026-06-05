"""MCP-backed implementation of :class:`KycClient`.

Wraps the seven ``madad_kyc_*`` tools the Phase 2 onboarding graph needs:

* ``madad_kyc_upload_commercial_registration`` — Step 2 (CR upload).
* ``madad_kyc_update_eligibility`` — Step 3 (eligibility intake form).
* ``madad_kyc_upload_audited_financial_report`` — Step 4 (financials).
* ``madad_kyc_get_admin_requested_documents`` — Step 5 (dynamic checklist).
* ``madad_kyc_upload_document_base64`` — Step 6 (documents upload loop).
* ``madad_kyc_add_buyer`` — Step 6 (buyer collection).
* ``madad_kyc_add_shareholders`` — Step 6 (shareholder collection).

Tool *arguments* are snake_case (Python-native, as in Ishan's ``fastmcp_tools``).
Responses are returned to callers as-is — the workflow nodes are the ones that
peel out the fields they care about.
"""

from __future__ import annotations

from typing import Any

from app.shared.mcp import MCPToolCaller, Tools


class McpKycClient:
    """MCP-backed implementation of the :class:`KycClient` port."""

    def __init__(self, tool_caller: MCPToolCaller) -> None:
        self._tools = tool_caller

    async def upload_commercial_registration(
        self, *, access_token: str, content_base64: str, filename: str
    ) -> dict[str, Any]:
        return await self._tools.call_tool(
            Tools.KYC_UPLOAD_COMMERCIAL_REGISTRATION,
            {
                "access_token": access_token,
                "content_base64": content_base64,
                "filename": filename,
            },
        )

    async def update_eligibility(
        self, *, access_token: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"access_token": access_token, **data}
        return await self._tools.call_tool(Tools.KYC_UPDATE_ELIGIBILITY, payload)

    async def upload_audited_financial_report(
        self, *, access_token: str, content_base64: str, filename: str
    ) -> dict[str, Any]:
        return await self._tools.call_tool(
            Tools.KYC_UPLOAD_AUDITED_FINANCIAL_REPORT,
            {
                "access_token": access_token,
                "content_base64": content_base64,
                "filename": filename,
            },
        )

    async def get_admin_requested_documents(
        self, *, access_token: str
    ) -> dict[str, Any]:
        return await self._tools.call_tool(
            Tools.KYC_GET_ADMIN_REQUESTED_DOCUMENTS, {"access_token": access_token}
        )

    async def upload_document_base64(
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        document_type: str,
    ) -> dict[str, Any]:
        return await self._tools.call_tool(
            Tools.KYC_UPLOAD_DOCUMENT_BASE64,
            {
                "access_token": access_token,
                "content_base64": content_base64,
                "filename": filename,
                "document_type": document_type,
            },
        )

    async def add_buyer(
        self, *, access_token: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"access_token": access_token, **data}
        return await self._tools.call_tool(Tools.KYC_ADD_BUYER, payload)

    async def add_shareholders(
        self, *, access_token: str, shareholders: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return await self._tools.call_tool(
            Tools.KYC_ADD_SHAREHOLDERS,
            {"access_token": access_token, "shareholders": shareholders},
        )
