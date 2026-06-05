"""MCP-backed implementations of the payment ports.

``McpMonetizationPaymentAdapter`` wraps the six tools the Phase 3 onboarding
payment block needs:

* ``madad_kyc_get_business_details`` — Step 5 (business record lookup before
  the payment is created — Madad needs the business id on the payment).
* ``madad_payments_list_monetization_products`` — picks the QAR 6,000
  monetization product to charge against.
* ``madad_payments_create_monetization_payment`` — the write that mints the
  payment record. Accepts an ``idempotency_key`` parameter — backend dedupes
  on it, so retries are safe.
* ``madad_payments_send_monetization_payment_link`` — sends the WhatsApp /
  email payment link to the SME. Also takes an ``idempotency_key``.
* ``madad_payments_get_monetization_payment`` — read the current payment
  record (used to confirm a webhook-claimed paid status).
* ``madad_payments_sync_monetization_payment_status`` — agent-initiated sync
  to the upstream payment provider; backstop for missed webhooks.

``McpTessLoanPaymentAdapter`` is a DEFERRED Phase-6 seam — only the
``transaction.disbursed`` webhook is listed in the catalog today (Ishan Q5).
Calling any method raises ``NotImplementedError`` so a misconfigured
workflow fails loudly instead of silently calling a tool that doesn't yet
exist.
"""

from __future__ import annotations

from typing import Any

from app.shared.mcp import MCPToolCaller, Tools
from app.shared.workflow.enums import Channel


class McpMonetizationPaymentAdapter:
    """MCP-backed implementation of the :class:`MonetizationPaymentClient` port."""

    def __init__(self, tool_caller: MCPToolCaller) -> None:
        self._tools = tool_caller

    async def get_business_details(self, *, access_token: str) -> dict[str, Any]:
        response = await self._tools.call_tool(
            Tools.KYC_GET_BUSINESS_DETAILS, {"access_token": access_token}
        )
        # UAT returns ``{success, businessDetails: {id, userId, legalEntityName,
        # crNumber, ...}}``. Workflow expects ``business_details_id`` at the
        # top level; unwrap the businessDetails sub-dict and alias ``id`` so
        # the caller doesn't need to know the actual response shape.
        details = response.get("businessDetails")
        if isinstance(details, dict):
            unwrapped: dict[str, Any] = dict(details)
            if "business_details_id" not in unwrapped and "id" in unwrapped:
                unwrapped["business_details_id"] = unwrapped["id"]
            return unwrapped
        return response

    async def list_monetization_products(
        self, *, access_token: str
    ) -> dict[str, Any]:
        out = await self._tools.call_tool(
            Tools.PAYMENTS_LIST_MONETIZATION_PRODUCTS,
            {"access_token": access_token},
        )
        # UAT returns the product set as the body of a ``{status_code, body}``
        # envelope where body is a BARE LIST (the universal unwrapper only
        # peels dict bodies). Normalize both shapes — and also a bare list
        # at the top — into the {"products": [...]} dict shape the workflow
        # expects.
        if isinstance(out, list):
            return {"products": out}
        if isinstance(out, dict) and isinstance(out.get("body"), list):
            return {"products": out["body"]}
        return out

    async def create_monetization_payment(
        self,
        *,
        access_token: str,
        business_details_id: str,
        product_id: str,
        amount_qar: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        # UAT names the amount field ``payable_amount`` (not the workflow's
        # internal ``amount_qar``).
        return await self._tools.call_tool(
            Tools.PAYMENTS_CREATE_MONETIZATION_PAYMENT,
            {
                "access_token": access_token,
                "idempotency_key": idempotency_key,
                "business_details_id": business_details_id,
                "product_id": product_id,
                "payable_amount": amount_qar,
            },
        )

    async def send_monetization_payment_link(
        self,
        *,
        access_token: str,
        payment_id: str,
        channel: Channel,
        identity: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        # UAT schema doesn't take a `channel` field; the recipient is
        # split into recipient_phone vs recipient_email and the backend
        # picks the channel from whichever is set.
        payload: dict[str, Any] = {
            "access_token": access_token,
            "idempotency_key": idempotency_key,
            "payment_id": payment_id,
        }
        if channel is Channel.EMAIL:
            payload["recipient_email"] = identity
        else:
            payload["recipient_phone"] = identity
        return await self._tools.call_tool(
            Tools.PAYMENTS_SEND_MONETIZATION_PAYMENT_LINK, payload
        )

    async def get_monetization_payment(
        self, *, access_token: str, payment_id: str
    ) -> dict[str, Any]:
        return await self._tools.call_tool(
            Tools.PAYMENTS_GET_MONETIZATION_PAYMENT,
            {"access_token": access_token, "payment_id": payment_id},
        )

    async def sync_monetization_payment_status(
        self, *, access_token: str, payment_id: str
    ) -> dict[str, Any]:
        return await self._tools.call_tool(
            Tools.PAYMENTS_SYNC_MONETIZATION_PAYMENT_STATUS,
            {"access_token": access_token, "payment_id": payment_id},
        )


class McpTessLoanPaymentAdapter:
    """[DEFERRED — pending Ishan Q5 / Phase 6 invoice financing]

    Placeholder for the Tess loan-disbursement MCP tools. Only the
    ``transaction.disbursed`` webhook event is listed in the catalog today;
    the corresponding tools ship in a later catalog cut. Until they land,
    every method raises ``NotImplementedError`` with the expected message
    so a misconfigured workflow fails loudly at the seam instead of silently
    calling a tool name that doesn't yet exist.

    Constructed with the same ``MCPToolCaller`` the other adapters use, so
    swapping the stub for a real implementation is a single file change.
    """

    def __init__(self, tool_caller: MCPToolCaller) -> None:
        self._tools = tool_caller

    async def disburse_loan(
        self, *, access_token: str, application_id: str
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "Tess loan-payment MCP tools not yet shipped (pending Ishan Q5 — "
            "only transaction.disbursed webhook listed today). Tracked in "
            "project_mcp_implementation_plan § Phase 6 (invoice financing)."
        )
