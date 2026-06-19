"""End-to-end capstone — drive the Phase 2 onboarding graph against a fake
MCP client wired through the production adapters.

Where the per-test files in ``tests/onboarding/`` exercise the workflow
against the in-memory port fakes, this capstone proves the FULL stack:

    OnboardingWorkflow
       → McpMadadIdentityClient (Phase 1 adapter)
       → McpKycClient (Phase 2 adapter)
           → InMemoryMCPClient (a stand-in for fastmcp.Client)

…so every camelCase-vs-snake_case translation, every required-arg shape, and
every tool-name constant has to be right end-to-end. If any of the adapter
seams regress, this test breaks before the per-adapter tests do.

The fake MCP client is constructed with handlers for every tool the new-lead
happy path invokes; the test asserts (a) the final outcome, (b) the exact
ordered tool-name sequence, and (c) a few specific payload shapes Ishan's
backend expects.
"""

from __future__ import annotations

from typing import Any

from app.services.workflow import (
    McpKycClient,
    McpMadadIdentityClient,
    McpMonetizationPaymentAdapter,
    RecordingMessenger,
    RecordingReminders,
    build_onboarding_platform,
)
from app.shared.mcp import InMemoryMCPClient, Tools
from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
IDENTITY = "+97455500500"


def _build_journey_handlers() -> dict[str, Any]:
    """Return a handler map driving the new-lead happy path end-to-end.

    The bridge tool returns an ``onboarding_token`` first and an
    ``access_token`` after ``complete_onboarding`` (Step-1 double-call). The
    ``auth_me`` handler is stateful: the test mutates ``state["journey_status"]``
    between turns to model the backend advancing.
    """

    state: dict[str, Any] = {
        "onboarding_complete": False,
        "journey_status": "ELIGIBLE",
    }

    def _create_channel_session(payload: dict[str, Any]) -> dict[str, Any]:
        # WhatsApp organic-entry: backend mints SIGN_UP + accessToken in one
        # call when create_user_if_missing=True is supplied.
        if payload.get("create_user_if_missing"):
            state["onboarding_complete"] = True
            return {
                "sessionType": "new_user_created",
                "accessToken": "AT-real-1",
                "refreshToken": "RT-real-1",
                "tokenExpiresAt": 1_800_000_000,
                "userOrLeadRef": "user_777",
                "referenceNumber": "Y6NICTES",
            }
        if state["onboarding_complete"]:
            return {
                "sessionType": "existing_user",
                "accessToken": "AT-real-1",
                "refreshToken": "RT-real-1",
                "tokenExpiresAt": 1_800_000_000,
                "userOrLeadRef": "user_777",
            }
        return {
            "sessionType": "new_lead",
            "onboardingToken": "OT-1",
            "userOrLeadRef": "lead_777",
        }

    def _check_contact(_p: dict[str, Any]) -> dict[str, Any]:
        return {"exists": False, "domainExists": False}

    def _complete_onboarding(_p: dict[str, Any]) -> dict[str, Any]:
        state["onboarding_complete"] = True
        return {"user": {"id": "user_777", "firstName": "Aisha", "lastName": "Karim"}}

    def _me(_p: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "user": {"id": "user_777", "journeyStatus": state["journey_status"]}
        }
        if state["journey_status"] in {"ACCEPTED", "OFFER_ACCEPTED"}:
            body["offers"] = [
                {"offer_id": "off-1", "lender": "Lender A"},
                {"offer_id": "off-2", "lender": "Lender B"},
            ]
        return body

    def _update_eligibility(_p: dict[str, Any]) -> dict[str, Any]:
        # Real cluster returns {success, journeyStatus} on the update —
        # workflow derives eligibility from journeyStatus != IN_ELIGIBLE.
        return {"success": True, "journeyStatus": "ELIGIBLE"}

    def _admin_requested(_p: dict[str, Any]) -> dict[str, Any]:
        uploaded: set[str] = state.setdefault("uploaded", set())
        required = ["trade_license", "tax_card"]
        missing = [code for code in required if code not in uploaded]
        return {"required": required, "missing": missing}

    def _upload_doc(p: dict[str, Any]) -> dict[str, Any]:
        # UAT schema: {file_name, mime_type, base64, metadata.{...}}.
        # The adapter SCREAMING-snake-cases workflow document_type when
        # writing metadata.document_type; lowercase back to the workflow
        # convention so the admin-requested-docs missing check converges.
        doc_type = p["metadata"]["document_type"].lower()
        state.setdefault("uploaded", set()).add(doc_type)
        return {"document_id": f"doc-{doc_type}"}

    return {
        "_state": state,
        Tools.MCP_CREATE_CHANNEL_SESSION: _create_channel_session,
        Tools.AUTH_CHECK_CONTACT: _check_contact,
        Tools.AUTH_COMPLETE_ONBOARDING: _complete_onboarding,
        Tools.AUTH_ME: _me,
        Tools.AUTH_REFRESH: lambda _p: {"accessToken": "AT-refresh", "refreshToken": "RT-refresh"},
        Tools.AUTH_LOGOUT: lambda _p: {},
        Tools.KYC_UPLOAD_COMMERCIAL_REGISTRATION: lambda p: {
            "document_id": "cr-1",
            "filename": p["filename"],
        },
        Tools.KYC_UPDATE_ELIGIBILITY: _update_eligibility,
        Tools.KYC_UPLOAD_AUDITED_FINANCIAL_REPORT: lambda p: {
            "document_id": "fr-1",
            "filename": p["filename"],
        },
        Tools.KYC_GET_ADMIN_REQUESTED_DOCUMENTS: _admin_requested,
        Tools.KYC_UPLOAD_DOCUMENT_BASE64: _upload_doc,
        Tools.KYC_ADD_BUYER: lambda p: {"buyer_id": "b-1", **p},
        Tools.KYC_ADD_SHAREHOLDERS: lambda p: {
            "shareholders": [
                {"shareholder_id": f"sh-{i}", **sh}
                for i, sh in enumerate(p["shareholders"])
            ]
        },
        # Phase 3 payment block. KYC_GET_BUSINESS_DETAILS now serves two
        # callers — _eligibility_update reads it back for state-sync, and
        # _business_details_fetch reads it for the payment chain. Wrap the
        # real response shape (businessDetails sub-dict) so the adapter's
        # unwrap-and-alias path is exercised here too.
        Tools.KYC_GET_BUSINESS_DETAILS: lambda _p: {
            "success": True,
            "businessDetails": {
                "id": "biz-1",
                "name": "Test SME",
                "legalEntityName": "Test SME LLC",
                "businessAge": "UNDER_2_YEARS",
                "crValidity": "UNDER_1_MONTH",
                "companyType": "LLC",
                "sector": "services",
                "turnover": "1000000",
                "employees": "10",
                "isQatarBased": True,
            },
        },
        Tools.PAYMENTS_LIST_MONETIZATION_PRODUCTS: lambda _p: {
            "products": [
                {
                    "product_id": "prod-monetization",
                    "name": "Onboarding Fee",
                    "payable_amount": 6000,
                }
            ]
        },
        # CREATE returns paymentLink + providerOrderNumber per the real
        # cluster shape; payment_send_link is now a side-channel.
        Tools.PAYMENTS_CREATE_MONETIZATION_PAYMENT: lambda p: {
            "payment_id": "pay-1",
            "status": "CREATED",
            "paymentLink": "https://pay.madad.example/pay-1",
            "providerOrderNumber": "MADAD-ONBOARDING-pay-1",
            "idempotency_key": p["idempotency_key"],
        },
        Tools.PAYMENTS_SEND_MONETIZATION_PAYMENT_LINK: lambda _p: {
            "payment_id": "pay-1",
            "payment_link": "https://pay.madad.example/pay-1",
        },
    }


async def test_full_new_lead_journey_through_real_mcp_adapters() -> None:
    handlers = _build_journey_handlers()
    backend_state = handlers.pop("_state")

    mcp = InMemoryMCPClient(handlers=handlers)
    platform = build_onboarding_platform(
        messenger=RecordingMessenger(),
        identity=McpMadadIdentityClient(mcp),
        kyc=McpKycClient(mcp),
        payments=McpMonetizationPaymentAdapter(mcp),
        reminders=RecordingReminders(),
    )
    runtime = platform.runtime

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})

    async def resume(message: dict[str, Any]) -> Any:
        return await runtime.resume(WA, IDENTITY, message=message)

    # Spec-aligned flow (post-2026-06-07): YES → consent_cr direct, CR → financials
    # direct, audited → PARK(prequalify_wait), webhook → documents, doc upload
    # → PARK(payment_wait), score event → payment chain.
    await resume({"text": "YES"})
    await resume({"text": "biz@example.com"})  # business_email
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": "QkE="}]})
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": "QkE="}]})
    await resume({"event": "prequalification.completed", "madadScore": 78})
    # Bug #10a + Bug #12 (2026-06-09): one madad_score.ready event exits
    # docs AND fast-forwards through payment_wait into the payment chain.
    await resume(
        {
            "attachments": [
                {"filename": "Establishment_Card.pdf", "content_base64": "QkE="},
            ]
        }
    )
    backend_state["journey_status"] = "QUALIFIED"
    pay_prompt = await resume(
        {"event": "madad_score.ready", "journey_status": "QUALIFIED"}
    )
    assert pay_prompt.prompt == {"waiting_for": "payment", "step": "payment"}

    # Mark monetization fee paid → lender_status_poll (still PRE_QUALIFIED) →
    # lender_wait.
    after_pay = await resume({"type": "payment", "paid": True})
    assert after_pay.prompt == {"waiting_for": "journey_status", "step": "lender_wait"}

    # Ishan 17c3d44 (2026-06-11): the run now parks after offer handoff so
    # post-handoff webhooks fire — drive through OFFER_ACCEPTED + ACTIVATED.
    backend_state["journey_status"] = "ACCEPTED"
    await resume({"type": "status_update"})
    backend_state["journey_status"] = "OFFER_ACCEPTED"
    await resume({"type": "status_update", "lenderName": "Qatar Islamic Bank"})
    backend_state["journey_status"] = "ACTIVATED"
    final = await resume({"type": "status_update", "lenderName": "Qatar Islamic Bank"})

    # Run stays open at invoice_collect after the credit line goes active.
    assert final.status == RunStatus.WAITING_FOR_INPUT
    assert final.prompt == {"waiting_for": "invoice", "step": "invoice_collect"}
    assert final.values["outcome"] == "completed"
    # offers were extracted from the auth_me embedded list.
    assert len(final.values["offers"]) == 2

    # Tool-name call order: every MCP tool the workflow needs is invoked
    # exactly when expected; nothing fabricated.
    call_names = [name for name, _ in mcp.calls]
    # CR + audited financial report ROUTE through KYC_UPLOAD_DOCUMENT_BASE64
    # (the specialised tools take file_path, not base64). The metadata
    # discriminator carries document_type=COMMERCIAL_REGISTRATION /
    # AUDITED_FINANCIAL_REPORT so the backend stores them under the right
    # entity slot.
    # Spec-aligned tool ordering (post-2026-06-07 merge): collect_details /
    # eligibility intake / buyer / shareholder steps are gone; doc upload uses
    # the same generic tool throughout.
    expected = {
        Tools.AUTH_CHECK_CONTACT,
        Tools.MCP_CREATE_CHANNEL_SESSION,
        Tools.KYC_UPLOAD_DOCUMENT_BASE64,
        Tools.AUTH_ME,
        Tools.KYC_GET_BUSINESS_DETAILS,
        Tools.PAYMENTS_LIST_MONETIZATION_PRODUCTS,
        Tools.PAYMENTS_CREATE_MONETIZATION_PAYMENT,
        # UAT 2026-06-19: PAYMENTS_SEND_MONETIZATION_PAYMENT_LINK dropped
        # (always 400, side-channel only — primary link goes via our own
        # messenger as a CTA-URL).
    }
    for tool in expected:
        assert tool in call_names, f"missing tool {tool} in {call_names}"
    assert Tools.PAYMENTS_SEND_MONETIZATION_PAYMENT_LINK not in call_names

    # The create-payment idempotency key is still recorded so retries
    # collapse on the same backend record.
    assert final.values["idempotency_keys"]["create_monetization_payment"].endswith(
        ":create_monetization_payment"
    )

