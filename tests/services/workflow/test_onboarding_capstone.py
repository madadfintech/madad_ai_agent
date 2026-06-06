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

    def _create_channel_session(_p: dict[str, Any]) -> dict[str, Any]:
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

    # Step 1: campaign → check_contact (new) → collect_details → complete →
    # second session.
    await resume({"text": "YES"})
    await resume({"first_name": "Aisha", "last_name": "Karim"})
    # Step 2: consent + CR upload.
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": "QkE="}]})
    # Step 3: eligibility intake form.
    await resume({"annual_revenue_qar": 5_000_000, "sector": "trade"})
    # Step 4: financials.
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": "QkE="}]})
    # Step 5–6: counterparties + documents.
    await resume({"name": "ACME LLC", "country": "QA"})
    await resume(
        {"shareholders": [{"name": "Aisha", "phoneNumber": "+97455500001"}]}
    )
    docs_done = await resume(
        {
            "attachments": [
                {"filename": "Trade_License.pdf", "content_base64": "QkE="},
                {"filename": "Tax_Card.pdf", "content_base64": "QkE="},
            ]
        }
    )
    assert docs_done.prompt == {"waiting_for": "journey_status", "step": "journey_wait"}

    # Step 7: advance the backend and resume — payment send → await.
    backend_state["journey_status"] = "PRE_QUALIFIED"
    pay_prompt = await resume({"type": "status_update"})
    assert pay_prompt.prompt == {"waiting_for": "payment", "step": "payment"}

    # Mark monetization fee paid → lender_status_poll (still PRE_QUALIFIED) →
    # lender_wait.
    after_pay = await resume({"type": "payment", "paid": True})
    assert after_pay.prompt == {"waiting_for": "journey_status", "step": "lender_wait"}

    # Backend advances → ACCEPTED → offers_fetch → offer_view → handoff.
    backend_state["journey_status"] = "ACCEPTED"
    final = await resume({"type": "status_update"})

    assert final.status == RunStatus.COMPLETED
    assert final.values["outcome"] == "offer_handoff"
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
    assert call_names == [
        Tools.AUTH_CHECK_CONTACT,
        Tools.MCP_CREATE_CHANNEL_SESSION,        # new-lead first bridge
        Tools.AUTH_COMPLETE_ONBOARDING,
        Tools.MCP_CREATE_CHANNEL_SESSION,        # second bridge (post-promotion)
        Tools.KYC_UPLOAD_DOCUMENT_BASE64,         # CR (routed via generic tool)
        Tools.KYC_UPDATE_ELIGIBILITY,
        Tools.KYC_GET_BUSINESS_DETAILS,           # state-sync after eligibility
        Tools.KYC_UPLOAD_DOCUMENT_BASE64,         # audited report (routed via generic tool)
        Tools.KYC_GET_ADMIN_REQUESTED_DOCUMENTS,  # documents_list_fetch
        Tools.KYC_ADD_BUYER,
        Tools.KYC_ADD_SHAREHOLDERS,
        Tools.KYC_UPLOAD_DOCUMENT_BASE64,         # trade_license
        Tools.KYC_UPLOAD_DOCUMENT_BASE64,         # tax_card
        Tools.KYC_GET_ADMIN_REQUESTED_DOCUMENTS,  # re-check missing
        Tools.AUTH_ME,                            # status_poll_on_demand (ELIGIBLE)
        Tools.AUTH_ME,                            # status_poll_on_demand (PRE_QUALIFIED)
        Tools.KYC_GET_BUSINESS_DETAILS,           # business_details_fetch (payment chain)
        Tools.PAYMENTS_LIST_MONETIZATION_PRODUCTS,
        Tools.PAYMENTS_CREATE_MONETIZATION_PAYMENT,
        Tools.PAYMENTS_SEND_MONETIZATION_PAYMENT_LINK,  # side-channel; failure absorbed
        Tools.AUTH_ME,                            # lender_status_poll (still PRE_QUALIFIED)
        Tools.AUTH_ME,                            # lender_status_poll (ACCEPTED)
        Tools.AUTH_ME,                            # offers_fetch
    ]

    # The idempotency keys we sent on the two payment writes are recorded in
    # state so the polling worker / audit can correlate retries.
    assert final.values["idempotency_keys"]["create_monetization_payment"].endswith(
        ":create_monetization_payment"
    )
    assert final.values["idempotency_keys"][
        "send_monetization_payment_link"
    ].endswith(":send_monetization_payment_link")


async def test_payloads_match_adapter_translation_at_the_seam() -> None:
    """Pin a few payload shapes the workflow hands to the MCP layer — these
    are the integration seams most likely to silently drift if either the
    adapters or the tool registry changes underneath us."""

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

    await resume({"text": "YES"})
    await resume({"first_name": "Aisha", "last_name": "Karim"})

    # check_contact called with phone (because the channel is WhatsApp).
    by_name: dict[str, list[dict[str, Any]]] = {}
    for name, payload in mcp.calls:
        by_name.setdefault(name, []).append(payload)
    assert by_name[Tools.AUTH_CHECK_CONTACT][0] == {"phone": IDENTITY}

    # The bridge tool receives uppercase channel + create_onboarding_token=True
    # on the FIRST (new-lead) call.
    first_bridge = by_name[Tools.MCP_CREATE_CHANNEL_SESSION][0]
    assert first_bridge["channel"] == "WHATSAPP"
    assert first_bridge["identifier"] == IDENTITY
    assert first_bridge["create_onboarding_token"] is True

    # complete_onboarding carries the onboarding_token from the first bridge,
    # plus the captured name + the channel as `phone` (UAT renames
    # phone_number → phone).
    complete = by_name[Tools.AUTH_COMPLETE_ONBOARDING][0]
    assert complete["onboarding_token"] == "OT-1"
    assert complete["first_name"] == "Aisha"
    assert complete["last_name"] == "Karim"
    assert complete["phone"] == IDENTITY

    # Second bridge call no longer asks for an onboarding_token.
    second_bridge = by_name[Tools.MCP_CREATE_CHANNEL_SESSION][1]
    assert second_bridge["create_onboarding_token"] is False

    # CR upload uses the UAT generic-base64 schema (file_name, mime_type,
    # base64, metadata{access_token, document_entity_type, document_type}).
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": "QkE="}]})
    cr = mcp.calls[-1][1]
    assert cr["file_name"] == "CR.pdf"
    assert cr["base64"] == "QkE="
    assert cr["mime_type"] == "application/pdf"
    assert cr["metadata"]["access_token"] == "AT-real-1"
    assert cr["metadata"]["document_type"] == "COMMERCIAL_REGISTRATION"

    # Eligibility form: workflow merges DEFAULT_ELIGIBILITY_FORM (the seven
    # canonical UAT fields) under any operator-supplied override values and
    # strips envelope keys before sending. We override `sector` and prove
    # the envelope `type` field doesn't reach the wire while the default
    # `is_qatar_based` is sent unchanged.
    await resume({"sector": "services", "type": "form"})
    # mcp.calls last entry is now KYC_GET_BUSINESS_DETAILS (state-sync read
    # after the eligibility update). Find the update call directly.
    eligibility = next(
        payload for name, payload in mcp.calls
        if name == "madad_kyc_update_eligibility"
    )
    assert eligibility["access_token"] == "AT-real-1"
    assert eligibility["sector"] == "services"               # operator override
    assert eligibility["is_qatar_based"] is True             # from defaults
    assert eligibility["business_age"] == "UNDER_2_YEARS"    # from defaults
    assert eligibility["company_type"] == "LLC"              # from defaults
    assert "type" not in eligibility                         # envelope stripped

    # journey_status flows through the camelCase response unwrapping.
    backend_state["journey_status"] = "PRE_QUALIFIED"
    # Skip the remaining workflow turns — already covered in the full-flow
    # test; this one just pins payload shapes.
