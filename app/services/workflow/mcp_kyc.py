"""MCP-backed implementation of :class:`KycClient` — aligned to the actual
UAT cluster contracts as discovered 2026-06-05.

Where the README and the InMemoryKycClient model a clean
``access_token + content_base64 + filename + document_type`` shape, the
real cluster requires per-tool variations:

* ``madad_kyc_upload_document_base64`` is a GENERIC base64-document upload —
  it takes ``{file_name, mime_type, base64, metadata}`` where ``metadata``
  is a free-form object whose required keys are
  ``access_token + document_entity_type + document_type``. The auth is
  inside the metadata, not at the top level.
* ``madad_kyc_upload_commercial_registration`` and
  ``madad_kyc_upload_audited_financial_report`` use ``file_path`` (a
  backend-resolvable path or URL, NOT base64). For staging-from-WhatsApp
  attachments where we only have base64 content, we route CR + financial
  report through the generic base64 tool with the appropriate
  ``document_type`` discriminator instead of these specialised tools.
* ``madad_kyc_update_eligibility`` requires SEVEN named fields
  (is_qatar_based, business_age, cr_validity, company_type, sector,
  turnover, employees) — not a free-form data dict. The adapter accepts
  the dict for compatibility with InMemoryKycClient + the eligibility
  intake node, and forwards only the required keys (extras passed through
  as additional properties, since the cluster's schema has
  ``additionalProperties: true`` for several KYC tools).
* ``madad_kyc_add_buyer`` requires ``name``; rest of the fields are
  optional and pass through (cr_number, contact_person, contact_number,
  contact_email, buyer_type, buyer_sector).

The dict[str, Any] return shape is preserved so InMemoryKycClient-keyed
tests keep working.
"""

from __future__ import annotations

from typing import Any

from app.shared.mcp import MCPToolCaller, Tools

# Madad's KYC backend addresses documents to a specific OWNER entity. For
# every onboarding-flow document the entity is the user's BUSINESS record;
# admin-requested docs and CR/financial uploads all flow under this entity.
DEFAULT_DOCUMENT_ENTITY_TYPE = "BUSINESS"

# Per-doc-type discriminators the backend expects on the generic base64
# upload tool. The keys are the workflow's internal document_type names
# (used in OnboardingState.missing_documents and elsewhere).
_DOCUMENT_TYPE_TO_BACKEND = {
    "trade_license": "TRADE_LICENSE",
    "commercial_registration": "COMMERCIAL_REGISTRATION",
    "audited_report": "AUDITED_FINANCIAL_REPORT",
    "bank_statement": "BANK_STATEMENT",
    "tax_card": "TAX_CARD",
    "establishment_card": "ESTABLISHMENT_CARD",
    "vat_certificate": "VAT_CERTIFICATE",
}


def _backend_document_type(workflow_doc_type: str) -> str:
    """Map the workflow's snake_case document_type to the backend's
    SCREAMING_SNAKE_CASE label. Unknown types pass through uppercased."""

    return _DOCUMENT_TYPE_TO_BACKEND.get(
        workflow_doc_type, workflow_doc_type.upper()
    )


class McpKycClient:
    """MCP-backed implementation of the :class:`KycClient` port."""

    def __init__(self, tool_caller: MCPToolCaller) -> None:
        self._tools = tool_caller

    async def upload_commercial_registration(
        self, *, access_token: str, content_base64: str, filename: str
    ) -> dict[str, Any]:
        # Route via the generic base64 tool with CR document_type — the
        # specialised CR tool takes file_path not base64, which we don't
        # have for WhatsApp-attachment payloads.
        return await self.upload_document_base64(
            access_token=access_token,
            content_base64=content_base64,
            filename=filename,
            document_type="commercial_registration",
        )

    async def update_eligibility(
        self, *, access_token: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        # The UAT tool requires the seven specific fields. The intake node
        # collects them into ``state.eligibility_form_data`` as a dict; we
        # forward the dict as-is so the cluster's pydantic validator sees
        # each named arg. Missing keys cause a 400 — the caller is
        # responsible for collecting all seven from the user.
        payload: dict[str, Any] = {"access_token": access_token, **data}
        return await self._tools.call_tool(Tools.KYC_UPDATE_ELIGIBILITY, payload)

    async def upload_audited_financial_report(
        self, *, access_token: str, content_base64: str, filename: str
    ) -> dict[str, Any]:
        # Same routing as CR — backend-specific tool wants file_path.
        return await self.upload_document_base64(
            access_token=access_token,
            content_base64=content_base64,
            filename=filename,
            document_type="audited_report",
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
                "file_name": filename,
                "mime_type": _infer_mime_type(filename),
                "base64": content_base64,
                "metadata": {
                    "access_token": access_token,
                    "document_entity_type": DEFAULT_DOCUMENT_ENTITY_TYPE,
                    "document_type": _backend_document_type(document_type),
                    "document_label": filename,
                },
            },
        )

    async def add_buyer(
        self, *, access_token: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        # The cluster requires `name`; other fields pass through.
        payload: dict[str, Any] = {"access_token": access_token, **data}
        return await self._tools.call_tool(Tools.KYC_ADD_BUYER, payload)

    async def add_shareholders(
        self, *, access_token: str, shareholders: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return await self._tools.call_tool(
            Tools.KYC_ADD_SHAREHOLDERS,
            {"access_token": access_token, "shareholders": shareholders},
        )


def _infer_mime_type(filename: str) -> str:
    """Pick a reasonable MIME type from the filename extension. The
    cluster validates the mime_type on KYC_UPLOAD_DOCUMENT_BASE64; we
    default to PDF since most onboarding documents are PDF, with a small
    table covering the common alternatives."""

    lowered = filename.lower()
    if lowered.endswith(".pdf"):
        return "application/pdf"
    if lowered.endswith(".png"):
        return "image/png"
    if lowered.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lowered.endswith(".csv"):
        return "text/csv"
    if lowered.endswith(".docx"):
        return (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
    if lowered.endswith(".xlsx"):
        return (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    return "application/pdf"
