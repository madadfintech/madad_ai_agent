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


def _normalise_invoice_envelope(
    response: Any, filename: str
) -> dict[str, Any]:
    """Flatten the cluster's response envelopes into one dict shape.

    The cluster may return ``{invoice: {...}}``, ``{body: {...}}``, or the
    invoice dict directly. We also alias ``id`` to ``invoice_id`` and
    inject the filename when missing — so every InvoiceClient path
    returns the same shape regardless of which underlying tool ran.
    """
    if isinstance(response, dict):
        if isinstance(response.get("invoice"), dict):
            normalised = dict(response["invoice"])
        elif isinstance(response.get("body"), dict):
            normalised = dict(response["body"])
        else:
            normalised = dict(response)
        if "invoice_id" not in normalised and "id" in normalised:
            normalised["invoice_id"] = normalised["id"]
        normalised.setdefault("filename", filename)
        return normalised
    return {"filename": filename, "raw": response}


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

    async def extract_base64(
        self,
        *,
        access_token: str,
        filename: str,
        content_base64: str,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        """Wraps ``madad_invoices_extract_invoice_base64`` — extract-only
        path for the confirm-card flow (UAT 2026-06-16 #3). Returns the
        OCR'd fields without creating a backend invoice record."""
        payload: dict[str, Any] = {
            "access_token": access_token,
            "file_name": filename,
            "file_base64": content_base64,
            "mime_type": mime_type or _infer_mime_type(filename),
        }
        response = await self._tools.call_tool(
            Tools.INVOICES_EXTRACT_INVOICE_BASE64, payload
        )
        return _normalise_invoice_envelope(response, filename)

    async def submit_base64(
        self,
        *,
        access_token: str,
        filename: str,
        content_base64: str,
        mime_type: str | None = None,
        user_id: str | None = None,
        status: str = "UNVERIFIED",
        invoice_number: str | None = None,
        invoice_date: str | None = None,
        due_date: str | None = None,
        total_amount: str | None = None,
        supplier_name: str | None = None,
        customer_name: str | None = None,
        line_items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Wraps ``madad_invoices_submit_invoice_base64`` — submit with
        explicit (possibly SME-edited) fields. The cluster signature
        carries 11 optional override fields; we forward whichever the
        agent has after confirm/edit, leaving the rest for the backend
        to fill from its own extraction."""
        payload: dict[str, Any] = {
            "access_token": access_token,
            "file_name": filename,
            "file_base64": content_base64,
            "mime_type": mime_type or _infer_mime_type(filename),
            "status": status,
        }
        # Forward only fields the caller actually supplied.
        for name, value in (
            ("user_id", user_id),
            ("invoice_number", invoice_number),
            ("invoice_date", invoice_date),
            ("due_date", due_date),
            ("total_amount", total_amount),
            ("supplier_name", supplier_name),
            ("customer_name", customer_name),
            ("line_items", line_items),
        ):
            if value is not None:
                payload[name] = value
        response = await self._tools.call_tool(
            Tools.INVOICES_SUBMIT_INVOICE_BASE64, payload
        )
        return _normalise_invoice_envelope(response, filename)

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
        # The cluster's ``madad_invoices_upload_zip`` accepts the same
        # ``file_name`` + ``file_base64`` shape as the single-invoice
        # base64 tool (see Ishan's cluster README §"Step 10"), so we
        # send the SME's WhatsApp ZIP bytes through the same tool. If
        # the cluster ever splits a separate ``upload_zip_base64`` tool,
        # this adapter is the only place that needs to switch.
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
        normalised = [i for i in invoices if isinstance(i, dict)]
        # ``**extras`` first so the canonical ``invoices`` / ``total``
        # keys can't be silently overwritten by a stray same-name field
        # in the upstream response.
        return {
            **extras,
            "invoices": normalised,
            "total": len(normalised),
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
