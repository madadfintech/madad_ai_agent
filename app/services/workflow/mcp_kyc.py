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

# Madad's KYC backend addresses documents to a specific OWNER entity. Per
# Ishan's enum (2026-06-05): for every onboarding-flow document the entity
# is the user's BUSINESS_DETAILS record; admin-requested docs and
# CR/financial uploads all flow under this entity. Other valid values:
# USER, KYC, SHAREHOLDER, DIRECTOR, BUYER, INVOICE, OFFER, TICKET,
# COMMUNICATION, BANK_DETAILS, AUTH_SIGNATORY, BENEFICIAL_OWNER.
DEFAULT_DOCUMENT_ENTITY_TYPE = "BUSINESS_DETAILS"

# Per-doc-type discriminators the backend expects on the generic base64
# upload tool. The keys are the workflow's internal document_type names
# (used in OnboardingState.missing_documents and elsewhere); the values MUST
# be exact members of the backend Prisma ``DocumentType`` enum — the upload
# endpoint does ``if (!(documentType in DocumentType)) throw BadRequest`` so an
# off-by-a-word guess (e.g. PAYABLE_AGEING vs PAYABLE_AGING_REPORT) returns 400
# and the document silently never lands. Verified against
# backend-api/src/prisma/schema.prisma (2026-06-07).
_DOCUMENT_TYPE_TO_BACKEND = {
    # Business registration docs
    "trade_license": "TRADE_LICENSE",
    "commercial_registration": "COMMERCIAL_REGISTRATION",
    "tax_card": "TAX_CARD",
    "establishment_card": "ESTABLISHMENT_CARD",
    "article_of_association": "ARTICLE_OF_ASSOCIATION",
    "national_address_certificate": "BUSINESS_ADDRESS_PROOF",
    "vat_certificate": "VAT_CERTIFICATE",
    # Financial docs (all BUSINESS_DETAILS entity)
    "audited_report": "AUDITED_FINANCIAL_REPORT",
    "bank_statement": "BANK_STATEMENT",
    "credit_bureau_report": "COMMERCIAL_CREDIT_REPORT",
    "payable_ageing": "PAYABLE_AGING_REPORT",
    "receivable_ageing": "RECEIVABLES_AGING_REPORT",
    "interim_statement": "INTERIM_FINANCIAL_REPORT",
    # Shareholder docs (SHAREHOLDER entity — see _SHAREHOLDER_DOCUMENT_TYPES)
    "qid": "SHAREHOLDER_QID",
    "passport": "SHAREHOLDER_PASSPORT",
    "proof_of_address": "SHAREHOLDER_PROOF_OF_ADDRESS",
}

# Workflow doc types whose backend home is the SHAREHOLDER entity rather than
# the business. We upload them WITHOUT a shareholderId: the backend parks them
# on the business and ``handleAsyncExtraction`` auto-assigns each to the
# matching shareholder by extracted ID — the same drag-and-drop path the
# msme-portal uses. (backend kyc.service.uploadDocuments, 2026-06-07.)
_SHAREHOLDER_DOCUMENT_TYPES = {"qid", "passport", "proof_of_address"}


def _backend_document_type(workflow_doc_type: str) -> str:
    """Map the workflow's snake_case document_type to the backend's
    SCREAMING_SNAKE_CASE label. Unknown types pass through uppercased."""

    return _DOCUMENT_TYPE_TO_BACKEND.get(
        workflow_doc_type, workflow_doc_type.upper()
    )


# Inverse of _DOCUMENT_TYPE_TO_BACKEND — backend SCREAMING_SNAKE → workflow snake.
# Built once at module load. When the backend's classify-and-upload tool returns
# a document_type we don't recognise (e.g. ``ADDITIONAL_DOCUMENT``), the caller
# should keep the file but flag it as unmapped.
_BACKEND_TO_DOCUMENT_TYPE = {v: k for k, v in _DOCUMENT_TYPE_TO_BACKEND.items()}


def workflow_doc_type(backend_doc_type: str) -> str:
    """Map a backend SCREAMING_SNAKE document_type back to the workflow's
    snake_case label. Unknown types lowercase-pass-through so the missing-
    docs accounting can still track them (just with a non-canonical name)."""

    return _BACKEND_TO_DOCUMENT_TYPE.get(
        backend_doc_type, backend_doc_type.lower()
    )


def _entity_type_for(workflow_doc_type: str) -> str:
    """The backend ``documentEntityType`` a given doc type belongs under."""

    if workflow_doc_type in _SHAREHOLDER_DOCUMENT_TYPES:
        return "SHAREHOLDER"
    return DEFAULT_DOCUMENT_ENTITY_TYPE


class McpKycClient:
    """MCP-backed implementation of the :class:`KycClient` port."""

    def __init__(self, tool_caller: MCPToolCaller) -> None:
        self._tools = tool_caller

    async def upload_commercial_registration(
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        # Route via the generic base64 tool with CR document_type — the
        # specialised CR tool takes file_path not base64, which we don't
        # have for WhatsApp-attachment payloads.
        return await self.upload_document_base64(
            access_token=access_token,
            content_base64=content_base64,
            filename=filename,
            document_type="commercial_registration",
            mime_type=mime_type,
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
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        # Same routing as CR — backend-specific tool wants file_path.
        return await self.upload_document_base64(
            access_token=access_token,
            content_base64=content_base64,
            filename=filename,
            document_type="audited_report",
            mime_type=mime_type,
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
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        return await self._tools.call_tool(
            Tools.KYC_UPLOAD_DOCUMENT_BASE64,
            {
                "file_name": filename,
                "mime_type": mime_type or _infer_mime_type(filename),
                "base64": content_base64,
                "metadata": {
                    "access_token": access_token,
                    "document_entity_type": _entity_type_for(document_type),
                    "document_type": _backend_document_type(document_type),
                    "document_label": filename,
                },
            },
        )

    async def classify_and_upload_document_base64(
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        mime_type: str | None = None,
        document_param: str | None = None,
        document_label: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "file_name": filename,
            "mime_type": mime_type or _infer_mime_type(filename),
            "base64": content_base64,
            "access_token": access_token,
        }
        if document_param is not None:
            payload["document_param"] = document_param
        if document_label is not None:
            payload["document_label"] = document_label
        return await self._tools.call_tool(
            Tools.KYC_CLASSIFY_AND_UPLOAD_DOCUMENT_BASE64, payload
        )

    async def classify_and_upload_zip_base64(
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        continue_on_error: bool = True,
    ) -> dict[str, Any]:
        return await self._tools.call_tool(
            Tools.KYC_CLASSIFY_AND_UPLOAD_ZIP_BASE64,
            {
                "file_name": filename,
                "base64": content_base64,
                "access_token": access_token,
                "continue_on_error": continue_on_error,
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
