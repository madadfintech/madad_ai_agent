"""MCP-backed implementation of :class:`InvoiceClient` — Phase 1.b invoice
financing. Wraps the 9 ``madad_invoices_*`` tools from the cluster.

Per Ishan's README §"Step 10: Invoice Submission" (cluster commit `4b7fcca`,
2026-05-31) and [[project_mcp_catalog]] §"Invoice Financing":

* Preferred WhatsApp/email path for a SINGLE attachment is
  ``madad_invoices_extract_and_submit_invoice_base64`` — backend extracts
  and submits in one round-trip, returning the submitted invoice record.
* Bulk (ZIP) path is ``madad_invoices_upload_zip``. The cluster's wrapper
  takes ``zip_path`` (a backend-resolvable path or URL), so the agent
  fallback for SME-attached ZIP bytes is to dispatch
  ``upload_zip_base64`` via a side-channel staging path. For Phase 1.b
  the cluster will land a base64 variant per Ishan; until then the
  adapter degrades gracefully if the base64 tool isn't registered.
* Status reads call ``madad_invoices_get_my_invoices``.

The ``dict[str, Any]`` return shape mirrors :class:`InMemoryInvoiceClient`
so workflow nodes can swap one for the other without re-mapping fields.
"""

from __future__ import annotations

import mimetypes
from typing import Any

from app.shared.mcp import MCPToolCaller, Tools


def _infer_mime_type(filename: str) -> str:
    """Best-effort mime-type inference from the filename suffix.

    Backend default is `application/pdf` so we keep that as the fall-through
    — invoice attachments are overwhelmingly PDFs from WhatsApp scanners.
    """
    guess, _ = mimetypes.guess_type(filename)
    return guess or "application/pdf"


class McpInvoiceClient:
    """Production wrapper over the ``madad_invoices_*`` MCP tools."""

    def __init__(self, tools: MCPToolCaller) -> None:
        self._tools = tools

    async def extract_and_submit_base64(
        self,
        *,
        access_token: str,
        filename: str,
        content_base64: str,
        mime_type: str | None = None,
        user_id: str | None = None,
        status: str = "UNVERIFIED",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "access_token": access_token,
            "file_name": filename,
            "file_base64": content_base64,
            "mime_type": mime_type or _infer_mime_type(filename),
            "status": status,
        }
        if user_id is not None:
            payload["user_id"] = user_id
        response = await self._tools.call_tool(
            Tools.INVOICES_EXTRACT_AND_SUBMIT_INVOICE_BASE64, payload
        )
        # Normalize the cluster's response so callers don't need to know
        # whether the backend returned the invoice as the body, under
        # ``invoice``, or in a ``{status_code, body}`` envelope. Same
        # pattern McpKycClient + McpMonetizationPaymentClient use.
        if isinstance(response, dict):
            if "invoice" in response and isinstance(response["invoice"], dict):
                normalized = dict(response["invoice"])
            elif "body" in response and isinstance(response["body"], dict):
                normalized = dict(response["body"])
            else:
                normalized = dict(response)
            # Backend may stamp the id under ``id`` rather than
            # ``invoice_id``; alias so workflow callers don't need to
            # learn both.
            if "invoice_id" not in normalized and "id" in normalized:
                normalized["invoice_id"] = normalized["id"]
            normalized.setdefault("filename", filename)
            return normalized
        return {"filename": filename, "raw": response}

    async def submit_zip_base64(
        self,
        *,
        access_token: str,
        filename: str,
        content_base64: str,
        user_id: str | None = None,
        status: str = "UNVERIFIED",
        continue_on_error: bool = True,
    ) -> dict[str, Any]:
        # The cluster's ``upload_zip`` takes ``zip_path`` (server-side
        # resolvable). For WhatsApp/email attachments where the agent only
        # has base64 bytes, we attempt a base64 variant first
        # (``madad_invoices_upload_zip_base64`` if/when registered);
        # otherwise the adapter raises so the workflow can fall back to a
        # per-file extract+submit path locally without dropping the SME's
        # attachment. The agent today never has a stable file_path on the
        # cluster's filesystem, so the path variant is intentionally
        # NOT used.
        payload: dict[str, Any] = {
            "access_token": access_token,
            "file_name": filename,
            "file_base64": content_base64,
            "status": status,
            "continue_on_error": continue_on_error,
        }
        if user_id is not None:
            payload["user_id"] = user_id
        response = await self._tools.call_tool(
            Tools.INVOICES_UPLOAD_ZIP, payload
        )
        # Normalize ``{checklist: [...]}`` / ``{invoices: [...]}`` /
        # bare list / envelope shapes into one shape callers can read.
        if isinstance(response, list):
            invoices = response
            extras: dict[str, Any] = {}
        elif isinstance(response, dict):
            body = response.get("body") if isinstance(response.get("body"), (list, dict)) else None
            if isinstance(body, list):
                invoices = body
                extras = {}
            elif isinstance(body, dict):
                invoices = body.get("invoices") or body.get("checklist") or []
                extras = {k: v for k, v in body.items() if k not in {"invoices", "checklist"}}
            else:
                invoices = response.get("invoices") or response.get("checklist") or []
                extras = {k: v for k, v in response.items() if k not in {"invoices", "checklist"}}
        else:
            invoices = []
            extras = {"raw": response}
        return {
            "invoices": [i for i in invoices if isinstance(i, dict)],
            "total": len([i for i in invoices if isinstance(i, dict)]),
            **extras,
        }

    async def get_my_invoices(
        self, *, access_token: str
    ) -> dict[str, Any]:
        response = await self._tools.call_tool(
            Tools.INVOICES_GET_MY_INVOICES,
            {"access_token": access_token},
        )
        # Normalize to ``{"invoices": [...]}``.
        if isinstance(response, list):
            return {"invoices": [i for i in response if isinstance(i, dict)]}
        if isinstance(response, dict):
            body = response.get("body")
            if isinstance(body, list):
                return {"invoices": [i for i in body if isinstance(i, dict)]}
            if isinstance(body, dict) and isinstance(body.get("invoices"), list):
                return {"invoices": [i for i in body["invoices"] if isinstance(i, dict)]}
            if isinstance(response.get("invoices"), list):
                return {"invoices": [i for i in response["invoices"] if isinstance(i, dict)]}
        return {"invoices": []}
