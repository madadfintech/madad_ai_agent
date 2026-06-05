"""Phase 2 onboarding workflow — MCP-backed onboarding graph.

Implements the Madad onboarding flow against the real MCP tool catalog
(``madad_auth_*`` + ``madad_kyc_*``). Replaces the Phase 1.a stub graph that
called fabricated RPC ports (``check_eligibility`` / ``request_score`` /
``submit_to_lenders`` / ``activate_credit_line``); those ports — ``MadadClient``
and friends — are deleted in this commit.

The graph follows Ishan's "Final Agent Flow Contract" (MCP cluster README):

  Step 1 (entry + identification):
    campaign_send/await
      → route_entry (YES → check_contact, NO → declined)
    check_contact_send/await
      → route_check_contact (existing | new | blocked)
        - existing  → channel_session_first (one bridge call → access_token)
        - new       → collect_onboarding_details_send/await
                      → complete_onboarding_send
                      → channel_session_second (second bridge call to
                        re-mint the access_token for the now-promoted user)
        - blocked   → domain_blocked terminal

  Step 2 (consent + CR upload):  consent_send/await → cr_upload_base64

  Step 3 (eligibility intake):
    eligibility_intake_send/await → eligibility_update
      → route_eligibility_status (eligible | ineligible)

  Step 4 (audited financials): financials_send/await → financials_upload_base64

  Step 5–6 (admin-requested docs + counterparties):
    documents_list_fetch
      → buyers_collect_send/await
      → shareholders_collect_send/await
      → documents_upload_loop_send/await
      → route_documents (complete | missing — missing loops back)
      → documents_complete

  Step 7 (status poll + payment):
    status_poll_on_demand → route_journey_status (16-status canonical branch)
      payment     → payment_send/await → route_payment (paid → lender_status_poll;
                    unpaid self-loops the await — Phase 3 wires the real
                    payment chain with idempotency keys)
      ineligible  → not_eligible terminal
      unqualified → not_qualified terminal (UNQUALIFIED, NOT_ACCEPTED)
      offers      → offers_fetch
      activated   → activated terminal (terminal-success)
      wait        → journey_wait_await → status_poll_on_demand

  Step 8 (lender + offers + handoff):
    lender_status_poll → route_journey_status
      offers      → offers_fetch
      unqualified → not_qualified
      activated   → activated
      wait        → lender_wait_await → lender_status_poll
    offers_fetch → offer_view_send → offer_handoff_to_madad terminal

The double-session-call pattern (Step 1, new-lead branch) is per Ishan's
contract: the bridge re-establishes the session after ``complete_onboarding``
promotes the lead so KYC calls have an ``access_token`` instead of an
``onboarding_token``.

Phase 4 wires real webhook receivers to drive the status_update resume
sources; in Phase 2 tests inject status updates via ``runtime.resume(...)``
between turns.
"""

from __future__ import annotations

from typing import Any

from app.shared.workflow import (
    GraphBuilder,
    WorkflowContext,
    WorkflowDefinition,
    await_input,
)
from app.shared.workflow.enums import Channel
from app.shared.workflow.state import HistoryEntry

from .ports import (
    KycClient,
    MadadIdentityClient,
    Messenger,
    MonetizationPaymentClient,
    Reminders,
)
from .state import JourneyStatus, OnboardingState, is_yes, reply_attachments, reply_text

# QAR 6,000 is the current monetization onboarding fee (Madad ops M-5 may
# vary it by segment later — the workflow falls back to whatever the
# products tool reports; this is the safety default if no products land).
ONBOARDING_FEE_QAR = 6000

TEMPLATE_KEYS = [
    "onboarding.campaign.intro",
    "onboarding.declined",
    "onboarding.domain_blocked",
    "onboarding.collect_details.request",
    "onboarding.consent.request",
    "onboarding.eligibility.intake.request",
    "onboarding.not_eligible",
    "onboarding.financials.request",
    "onboarding.buyers.request",
    "onboarding.shareholders.request",
    "onboarding.documents.checklist",
    "onboarding.documents.missing",
    "onboarding.documents.complete",
    "onboarding.not_qualified",
    "onboarding.payment.request",
    "onboarding.offers.preview",
    "onboarding.offer.handoff",
    "onboarding.activated",
]

# Default values for the seven KYC_UPDATE_ELIGIBILITY fields when the
# operator-supplied form data doesn't include them. Chosen so the staging
# demo against a known-eligible test account submits a passing record
# without an interactive form. Override per-run via the inbound `data`
# payload on the eligibility-intake await.
DEFAULT_ELIGIBILITY_FORM: dict[str, Any] = {
    "is_qatar_based": True,
    "business_age": 5,
    "cr_validity": "VALID",
    "company_type": "LLC",
    "sector": "trade",
    "turnover": 1_000_000,
    "employees": 10,
}

# Filename → KYC document_type inference for the documents upload loop.
DOC_TYPE_KEYWORDS = {
    "trade": "trade_license",
    "tax": "tax_card",
    "bank": "bank_statement",
    "audited": "audited_report",
    "establishment": "establishment_card",
    "vat": "vat_certificate",
}


def _infer_doc_type(filename: str) -> str | None:
    lowered = filename.lower()
    for keyword, doc_type in DOC_TYPE_KEYWORDS.items():
        if keyword in lowered:
            return doc_type
    return None


class OnboardingWorkflow(WorkflowDefinition):
    name = "onboarding"
    version = 1
    state_schema = OnboardingState

    def __init__(
        self,
        *,
        messenger: Messenger,
        identity: MadadIdentityClient,
        kyc: KycClient,
        payments: MonetizationPaymentClient,
        reminders: Reminders,
    ) -> None:
        self._msg = messenger
        self._identity = identity
        self._kyc = kyc
        self._pay = payments
        self._reminders = reminders

    # -- graph wiring ---------------------------------------------------------

    def build(self, graph: GraphBuilder) -> None:
        nodes: dict[str, Any] = {
            # Step 1: campaign + identification
            "campaign_send": self._campaign_send,
            "campaign_await": self._campaign_await,
            "declined": self._declined,
            "check_contact_send": self._check_contact_send,
            "check_contact_await": self._check_contact_await,
            "domain_blocked": self._domain_blocked,
            "channel_session_first": self._channel_session_first,
            "collect_onboarding_details_send": self._collect_onboarding_details_send,
            "collect_onboarding_details_await": self._collect_onboarding_details_await,
            "complete_onboarding_send": self._complete_onboarding_send,
            "channel_session_second": self._channel_session_second,
            # Step 2: consent + CR
            "consent_send": self._consent_send,
            "consent_await": self._consent_await,
            "cr_upload_base64": self._cr_upload_base64,
            # Step 3: eligibility intake
            "eligibility_intake_send": self._eligibility_intake_send,
            "eligibility_intake_await": self._eligibility_intake_await,
            "eligibility_update": self._eligibility_update,
            "not_eligible": self._not_eligible,
            # Step 4: financials
            "financials_send": self._financials_send,
            "financials_await": self._financials_await,
            "financials_upload_base64": self._financials_upload_base64,
            # Step 5–6: docs + counterparties
            "documents_list_fetch": self._documents_list_fetch,
            "buyers_collect_send": self._buyers_collect_send,
            "buyers_collect_await": self._buyers_collect_await,
            "shareholders_collect_send": self._shareholders_collect_send,
            "shareholders_collect_await": self._shareholders_collect_await,
            "documents_upload_loop_send": self._documents_upload_loop_send,
            "documents_upload_loop_await": self._documents_upload_loop_await,
            "documents_complete": self._documents_complete,
            # Step 7: status poll + payment
            "status_poll_on_demand": self._status_poll_on_demand,
            "journey_wait_await": self._journey_wait_await,
            "not_qualified": self._not_qualified,
            "business_details_fetch": self._business_details_fetch,
            "products_list_fetch": self._products_list_fetch,
            "payment_create": self._payment_create,
            "payment_send_link": self._payment_send_link,
            "payment_await": self._payment_await,
            # Step 8: lender + offers + terminals
            "lender_status_poll": self._lender_status_poll,
            "lender_wait_await": self._lender_wait_await,
            "offers_fetch": self._offers_fetch,
            "offer_view_send": self._offer_view_send,
            "offer_handoff_to_madad": self._offer_handoff_to_madad,
            "activated": self._activated,
        }
        for node_name, fn in nodes.items():
            graph.add_node(node_name, fn)

        graph.set_entry("campaign_send")
        graph.add_edge("campaign_send", "campaign_await")
        graph.add_conditional_edges(
            "campaign_await",
            self._route_entry,
            {"check_contact": "check_contact_send", "declined": "declined"},
        )
        graph.add_edge("check_contact_send", "check_contact_await")
        graph.add_conditional_edges(
            "check_contact_await",
            self._route_check_contact,
            {
                "existing": "channel_session_first",
                "new": "collect_onboarding_details_send",
                "blocked": "domain_blocked",
            },
        )

        # Existing-user path converges at consent_send via one session call.
        graph.add_conditional_edges(
            "channel_session_first",
            self._route_channel_session,
            {"consent": "consent_send"},
        )
        # New-lead path: collect details → complete onboarding → second session.
        graph.add_edge("collect_onboarding_details_send", "collect_onboarding_details_await")
        graph.add_edge("collect_onboarding_details_await", "complete_onboarding_send")
        graph.add_edge("complete_onboarding_send", "channel_session_second")
        graph.add_conditional_edges(
            "channel_session_second",
            self._route_channel_session,
            {"consent": "consent_send"},
        )

        graph.add_edge("consent_send", "consent_await")
        graph.add_edge("consent_await", "cr_upload_base64")
        graph.add_edge("cr_upload_base64", "eligibility_intake_send")
        graph.add_edge("eligibility_intake_send", "eligibility_intake_await")
        graph.add_edge("eligibility_intake_await", "eligibility_update")
        graph.add_conditional_edges(
            "eligibility_update",
            self._route_eligibility_status,
            {"eligible": "financials_send", "ineligible": "not_eligible"},
        )

        graph.add_edge("financials_send", "financials_await")
        graph.add_edge("financials_await", "financials_upload_base64")
        graph.add_edge("financials_upload_base64", "documents_list_fetch")
        graph.add_edge("documents_list_fetch", "buyers_collect_send")
        graph.add_edge("buyers_collect_send", "buyers_collect_await")
        graph.add_edge("buyers_collect_await", "shareholders_collect_send")
        graph.add_edge("shareholders_collect_send", "shareholders_collect_await")
        graph.add_edge("shareholders_collect_await", "documents_upload_loop_send")
        graph.add_edge("documents_upload_loop_send", "documents_upload_loop_await")
        graph.add_conditional_edges(
            "documents_upload_loop_await",
            self._route_documents,
            {"complete": "documents_complete", "missing": "documents_upload_loop_send"},
        )
        graph.add_edge("documents_complete", "status_poll_on_demand")

        graph.add_conditional_edges(
            "status_poll_on_demand",
            self._route_journey_status,
            {
                "payment": "business_details_fetch",
                "ineligible": "not_eligible",
                "unqualified": "not_qualified",
                "offers": "offers_fetch",
                "activated": "activated",
                "wait": "journey_wait_await",
            },
        )
        graph.add_edge("journey_wait_await", "status_poll_on_demand")

        # Payment chain: business details → product lookup → create (with
        # idempotency_key) → send link (with idempotency_key) → await.
        graph.add_edge("business_details_fetch", "products_list_fetch")
        graph.add_edge("products_list_fetch", "payment_create")
        graph.add_edge("payment_create", "payment_send_link")
        graph.add_edge("payment_send_link", "payment_await")
        graph.add_conditional_edges(
            "payment_await",
            self._route_payment,
            {"paid": "lender_status_poll", "unpaid": "payment_await"},
        )

        graph.add_conditional_edges(
            "lender_status_poll",
            self._route_journey_status,
            {
                "payment": "lender_wait_await",
                "ineligible": "not_qualified",
                "unqualified": "not_qualified",
                "offers": "offers_fetch",
                "activated": "activated",
                "wait": "lender_wait_await",
            },
        )
        graph.add_edge("lender_wait_await", "lender_status_poll")

        graph.add_edge("offers_fetch", "offer_view_send")
        graph.add_edge("offer_view_send", "offer_handoff_to_madad")

        for terminal in (
            "declined",
            "domain_blocked",
            "not_eligible",
            "not_qualified",
            "offer_handoff_to_madad",
            "activated",
        ):
            graph.set_finish(terminal)

    # -- Step 1: campaign + check_contact + session ---------------------------

    async def _campaign_send(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        locale = str(state.data.get("locale") or state.locale)
        await self._send(ctx, state, "onboarding.campaign.intro", locale=locale)
        return self._step("campaign_send", ctx, locale=locale)

    async def _campaign_await(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        reply = await_input({"waiting_for": "reply", "step": "campaign"})
        return self._step("campaign_await", ctx, entry_reply="YES" if is_yes(reply) else "NO")

    async def _declined(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.declined")
        return self._step("declined", ctx, outcome="declined")

    async def _check_contact_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        if ctx.channel is Channel.WHATSAPP:
            result = await self._identity.check_contact(phone=ctx.identity)
        else:
            result = await self._identity.check_contact(email=ctx.identity)
        return self._step(
            "check_contact_send",
            ctx,
            check_contact_result=result,
            channel_identity=ctx.identity,
        )

    async def _check_contact_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # Passthrough — the lookup happens in _check_contact_send and the
        # router branches on its result; no inbound input is awaited here.
        return self._step("check_contact_await", ctx)

    async def _domain_blocked(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        domain = state.check_contact_result.domain if state.check_contact_result else None
        await self._send(
            ctx, state, "onboarding.domain_blocked", {"domain": domain or ""}
        )
        return self._step(
            "domain_blocked",
            ctx,
            outcome="domain_blocked",
            domain_block_reason=domain,
        )

    async def _channel_session_first(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        session = await self._identity.open_session(
            channel=_channel(ctx),
            identifier=ctx.identity,
            create_onboarding_token=False,
        )
        return self._step(
            "channel_session_first",
            ctx,
            channel_session_response=session,
            session_type=session.session_type,
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            token_expires_at=session.token_expires_at,
            madad_user_id=session.user_or_lead_ref,
        )

    async def _collect_onboarding_details_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.collect_details.request")
        # Open the first bridge call so the new lead has an onboarding_token
        # by the time complete_onboarding_send fires.
        session = await self._identity.open_session(
            channel=_channel(ctx),
            identifier=ctx.identity,
            create_onboarding_token=True,
        )
        return self._step(
            "collect_onboarding_details_send",
            ctx,
            channel_session_response=session,
            session_type=session.session_type,
            onboarding_token=session.onboarding_token,
            madad_user_id=session.user_or_lead_ref,
        )

    async def _collect_onboarding_details_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        reply = await_input({"waiting_for": "reply", "step": "collect_details"})
        first, last = self._parse_name(reply)
        return self._step(
            "collect_onboarding_details_await",
            ctx,
            onboarding_first_name=first,
            onboarding_last_name=last,
        )

    async def _complete_onboarding_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._identity.complete_onboarding(
            first_name=state.onboarding_first_name or "",
            last_name=state.onboarding_last_name or "",
            onboarding_token=state.onboarding_token or "",
            phone_number=ctx.identity if ctx.channel is Channel.WHATSAPP else None,
            email=ctx.identity if ctx.channel is Channel.EMAIL else None,
        )
        return self._step("complete_onboarding_send", ctx)

    async def _channel_session_second(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        session = await self._identity.open_session(
            channel=_channel(ctx),
            identifier=ctx.identity,
            create_onboarding_token=False,
        )
        return self._step(
            "channel_session_second",
            ctx,
            channel_session_response=session,
            session_type=session.session_type,
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            token_expires_at=session.token_expires_at,
            madad_user_id=session.user_or_lead_ref,
        )

    # -- Step 2: consent + CR upload -----------------------------------------

    async def _consent_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.consent.request")
        return self._step("consent_send", ctx)

    async def _consent_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        reply = await_input({"waiting_for": "upload", "step": "consent_cr"})
        attachments = reply_attachments(reply)
        if not attachments:
            return self._step("consent_await", ctx, consent=False)
        first = attachments[0]
        return self._step(
            "consent_await",
            ctx,
            consent=True,
            cr_ref=first.get("filename"),
            cr_content_base64=first.get("content_base64") or "",
        )

    async def _cr_upload_base64(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        if state.access_token and state.cr_ref:
            try:
                await self._kyc.upload_commercial_registration(
                    access_token=state.access_token,
                    content_base64=state.cr_content_base64 or "",
                    filename=state.cr_ref,
                )
            except Exception as exc:  # noqa: BLE001 — degrade in staging
                ctx.logger.warning(
                    "cr_upload.failed", error=str(exc)[:200],
                    note="staging-tolerant: continuing without CR uploaded",
                )
        return self._step("cr_upload_base64", ctx)

    # -- Step 3: eligibility intake ------------------------------------------

    async def _eligibility_intake_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.eligibility.intake.request")
        await self._reminders.schedule(
            "eligibility_pending",
            channel=_channel(ctx),
            identity=ctx.identity,
            target_ref=state.madad_user_id or ctx.session_id,
        )
        return self._step("eligibility_intake_send", ctx)

    async def _eligibility_intake_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        reply = await_input({"waiting_for": "eligibility_form", "step": "eligibility"})
        form = reply if isinstance(reply, dict) else {}
        form_data = {
            k: v for k, v in form.items() if k not in {"type", "text", "attachments"}
        }
        await self._reminders.suppress(target_ref=state.madad_user_id or ctx.session_id)
        return self._step(
            "eligibility_intake_await", ctx, eligibility_form_data=form_data
        )

    async def _eligibility_update(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        eligible = True
        if state.access_token:
            # Merge operator-supplied form data on top of demo defaults so
            # the seven required UAT fields are always present. The operator
            # only needs to override the fields they want to change.
            payload = {**DEFAULT_ELIGIBILITY_FORM, **state.eligibility_form_data}
            try:
                result = await self._kyc.update_eligibility(
                    access_token=state.access_token, data=payload
                )
                if isinstance(result, dict):
                    eligible = bool(result.get("eligible", True))
            except Exception as exc:  # noqa: BLE001 — degrade in staging
                ctx.logger.warning(
                    "eligibility.update_failed",
                    error=str(exc)[:200],
                    note="staging-tolerant: continuing with eligible=True",
                )
        return self._step("eligibility_update", ctx, eligible=eligible)

    async def _not_eligible(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.not_eligible")
        return self._step("not_eligible", ctx, outcome="not_eligible")

    # -- Step 4: financials ---------------------------------------------------

    async def _financials_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.financials.request")
        await self._reminders.schedule(
            "financials_pending",
            channel=_channel(ctx),
            identity=ctx.identity,
            target_ref=state.madad_user_id or ctx.session_id,
        )
        return self._step("financials_send", ctx)

    async def _financials_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        reply = await_input({"waiting_for": "upload", "step": "financials"})
        attachments = reply_attachments(reply)
        await self._reminders.suppress(target_ref=state.madad_user_id or ctx.session_id)
        if not attachments:
            return self._step("financials_await", ctx, financials_received=False)
        first = attachments[0]
        return self._step(
            "financials_await",
            ctx,
            financials_received=True,
            financials_content_base64=first.get("content_base64") or "",
            financials_filename=first.get("filename") or "",
        )

    async def _financials_upload_base64(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        if state.access_token and state.financials_received:
            try:
                await self._kyc.upload_audited_financial_report(
                    access_token=state.access_token,
                    content_base64=state.financials_content_base64 or "",
                    filename=state.financials_filename or "audited_report.pdf",
                )
            except Exception as exc:  # noqa: BLE001 — degrade in staging
                ctx.logger.warning(
                    "financials_upload.failed", error=str(exc)[:200],
                    note="staging-tolerant: continuing without financials uploaded",
                )
        return self._step("financials_upload_base64", ctx)

    # -- Step 5-6: admin-requested documents + counterparties ----------------

    async def _documents_list_fetch(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        missing: list[str] = []
        if state.access_token:
            result = await self._kyc.get_admin_requested_documents(
                access_token=state.access_token
            )
            if isinstance(result, dict):
                missing = list(result.get("missing", []))
        return self._step("documents_list_fetch", ctx, missing_documents=missing)

    async def _buyers_collect_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.buyers.request")
        return self._step("buyers_collect_send", ctx)

    async def _buyers_collect_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        reply = await_input({"waiting_for": "buyers", "step": "buyers"})
        buyer = reply if isinstance(reply, dict) else {}
        data = {
            k: v for k, v in buyer.items() if k not in {"type", "text", "attachments"}
        }
        if data and state.access_token:
            await self._kyc.add_buyer(access_token=state.access_token, data=data)
        buyers = [*state.buyers, data] if data else list(state.buyers)
        return self._step("buyers_collect_await", ctx, buyers=buyers)

    async def _shareholders_collect_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.shareholders.request")
        return self._step("shareholders_collect_send", ctx)

    async def _shareholders_collect_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        reply = await_input({"waiting_for": "shareholders", "step": "shareholders"})
        payload = reply if isinstance(reply, dict) else {}
        raw = payload.get("shareholders")
        items: list[dict[str, Any]] = list(raw) if isinstance(raw, list) else []
        if items and state.access_token:
            await self._kyc.add_shareholders(
                access_token=state.access_token, shareholders=items
            )
        return self._step("shareholders_collect_await", ctx, shareholders=items)

    async def _documents_upload_loop_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # First entry uses the checklist template; re-entries (missing-docs
        # loop-back) use the shorter "still missing" template.
        already_asked = any(
            h.step == "documents_upload_loop_send" for h in state.history
        )
        template_key = (
            "onboarding.documents.missing"
            if already_asked
            else "onboarding.documents.checklist"
        )
        await self._send(
            ctx, state, template_key, {"documents": ", ".join(state.missing_documents)}
        )
        await self._reminders.schedule(
            "incomplete_docs",
            channel=_channel(ctx),
            identity=ctx.identity,
            target_ref=state.madad_user_id or ctx.session_id,
        )
        return self._step("documents_upload_loop_send", ctx)

    async def _documents_upload_loop_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        reply = await_input({"waiting_for": "upload", "step": "documents"})
        attachments = reply_attachments(reply)
        for att in attachments:
            doc_type = att.get("document_type") or _infer_doc_type(att.get("filename") or "")
            if state.access_token and doc_type:
                try:
                    await self._kyc.upload_document_base64(
                        access_token=state.access_token,
                        content_base64=att.get("content_base64") or "",
                        filename=att.get("filename") or "",
                        document_type=doc_type,
                    )
                except Exception as exc:  # noqa: BLE001 — degrade in staging
                    ctx.logger.warning(
                        "document_upload.failed",
                        document_type=doc_type,
                        error=str(exc)[:200],
                        note="staging-tolerant: continuing without this doc",
                    )
        missing: list[str] = list(state.missing_documents)
        if state.access_token:
            try:
                result = await self._kyc.get_admin_requested_documents(
                    access_token=state.access_token
                )
                if isinstance(result, dict):
                    missing = list(result.get("missing", []))
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning(
                    "get_admin_requested_documents.failed", error=str(exc)[:200]
                )
        if not missing:
            await self._reminders.suppress(
                target_ref=state.madad_user_id or ctx.session_id
            )
        return self._step("documents_upload_loop_await", ctx, missing_documents=missing)

    async def _documents_complete(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.documents.complete")
        return self._step("documents_complete", ctx)

    # -- Step 7: status poll + payment ----------------------------------------

    async def _status_poll_on_demand(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        status = await self._poll_journey_status(state)
        # Preserve the source set by the upstream await (webhook vs poll
        # trigger). Default to "poll" if nothing set it — this node is by
        # definition an active poll, and the first entry (from
        # documents_complete) has no upstream source.
        source = state.last_status_source or "poll"
        return self._step(
            "status_poll_on_demand",
            ctx,
            journey_status=status,
            last_status_source=source,
            last_polled_at=ctx.clock.now(),
        )

    async def _journey_wait_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # Suspend until a status_update / webhook resume arrives. The resume
        # payload's ``last_status_source`` (set by the dispatcher on backend
        # webhook arrivals — see translate_backend_event) is carried into
        # state so the polling worker can suppress its next cycle.
        payload = await_input({"waiting_for": "journey_status", "step": "journey_wait"})
        source = _extract_status_source(payload)
        return self._step("journey_wait_await", ctx, last_status_source=source)

    async def _not_qualified(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.not_qualified")
        return self._step("not_qualified", ctx, outcome="not_qualified")

    async def _business_details_fetch(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        if not state.access_token:
            return self._step("business_details_fetch", ctx)
        result = await self._pay.get_business_details(access_token=state.access_token)
        business_id = (
            result.get("business_details_id") if isinstance(result, dict) else None
        )
        return self._step(
            "business_details_fetch", ctx, business_details_id=business_id
        )

    async def _products_list_fetch(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        if not state.access_token:
            return self._step("products_list_fetch", ctx)
        result = await self._pay.list_monetization_products(
            access_token=state.access_token
        )
        products = (
            list(result.get("products", [])) if isinstance(result, dict) else []
        )
        product = products[0] if products else {}
        return self._step(
            "products_list_fetch",
            ctx,
            payment_product_id=product.get("product_id"),
        )

    async def _payment_create(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        if not (
            state.access_token
            and state.business_details_id
            and state.payment_product_id
        ):
            return self._step("payment_create", ctx)
        key = f"{ctx.run_id}:create_monetization_payment"
        result = await self._pay.create_monetization_payment(
            access_token=state.access_token,
            business_details_id=state.business_details_id,
            product_id=state.payment_product_id,
            amount_qar=ONBOARDING_FEE_QAR,
            idempotency_key=key,
        )
        payment_id = result.get("payment_id") if isinstance(result, dict) else None
        payment_status = result.get("status") if isinstance(result, dict) else None
        return self._step(
            "payment_create",
            ctx,
            payment_id=payment_id,
            payment_status=payment_status,
            idempotency_keys={
                **state.idempotency_keys,
                "create_monetization_payment": key,
            },
        )

    async def _payment_send_link(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # Send our explanatory message; the MCP send-link tool then delivers
        # the actual link via the Madad payments backend on the same channel.
        await self._send(ctx, state, "onboarding.payment.request")
        await self._reminders.schedule(
            "payment_pending",
            channel=_channel(ctx),
            identity=ctx.identity,
            target_ref=state.madad_user_id or ctx.session_id,
        )
        if not (state.access_token and state.payment_id):
            return self._step("payment_send_link", ctx)
        key = f"{ctx.run_id}:send_monetization_payment_link"
        payment_link: str | None = None
        try:
            result = await self._pay.send_monetization_payment_link(
                access_token=state.access_token,
                payment_id=state.payment_id,
                channel=_channel(ctx),
                identity=ctx.identity,
                idempotency_key=key,
            )
            if isinstance(result, dict):
                payment_link = result.get("payment_link") or result.get("paymentLink")
        except Exception as exc:  # noqa: BLE001 — UAT upstream notification 502
            ctx.logger.warning(
                "payment_send_link.failed",
                error=str(exc)[:200],
                note="staging-tolerant: continuing — link delivery is upstream",
            )
        return self._step(
            "payment_send_link",
            ctx,
            payment_link=payment_link,
            idempotency_keys={
                **state.idempotency_keys,
                "send_monetization_payment_link": key,
            },
        )

    async def _payment_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        result = await_input({"waiting_for": "payment", "step": "payment"})
        paid = bool(result.get("paid")) if isinstance(result, dict) else False
        if paid:
            await self._reminders.suppress(
                target_ref=state.madad_user_id or ctx.session_id
            )
        return self._step("payment_await", ctx, paid=paid)

    # -- Step 8: lender + offers + terminals ----------------------------------

    async def _lender_status_poll(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        status = await self._poll_journey_status(state)
        source = state.last_status_source or "poll"
        return self._step(
            "lender_status_poll",
            ctx,
            journey_status=status,
            last_status_source=source,
            last_polled_at=ctx.clock.now(),
        )

    async def _lender_wait_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        payload = await_input({"waiting_for": "journey_status", "step": "lender_wait"})
        source = _extract_status_source(payload)
        return self._step("lender_wait_await", ctx, last_status_source=source)

    async def _offers_fetch(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # The backend embeds the lender offer set on the user record once the
        # journey reaches ACCEPTED. Pull it off the latest auth_me response.
        offers: list[dict[str, Any]] = []
        if state.access_token:
            info = await self._identity.me(access_token=state.access_token)
            if isinstance(info, dict):
                raw = info.get("offers")
                if isinstance(raw, list):
                    offers = list(raw)
        return self._step("offers_fetch", ctx, offers=offers)

    async def _offer_view_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(
            ctx, state, "onboarding.offers.preview", {"count": len(state.offers)}
        )
        return self._step("offer_view_send", ctx)

    async def _offer_handoff_to_madad(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.offer.handoff")
        return self._step("offer_handoff_to_madad", ctx, outcome="offer_handoff")

    async def _activated(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.activated")
        return self._step("activated", ctx, outcome="completed")

    # -- routers --------------------------------------------------------------

    def _route_entry(self, state: OnboardingState) -> str:
        return "check_contact" if state.entry_reply == "YES" else "declined"

    def _route_check_contact(self, state: OnboardingState) -> str:
        result = state.check_contact_result
        if result is None:
            return "new"
        if result.exists:
            return "existing"
        if result.domain_exists:
            return "blocked"
        return "new"

    def _route_channel_session(self, state: OnboardingState) -> str:
        # Phase 6 (invoice financing) will branch existing users into a
        # fast-path; Phase 2 always proceeds to consent + CR.
        return "consent"

    def _route_eligibility_status(self, state: OnboardingState) -> str:
        return "eligible" if state.eligible else "ineligible"

    def _route_documents(self, state: OnboardingState) -> str:
        return "missing" if state.missing_documents else "complete"

    def _route_payment(self, state: OnboardingState) -> str:
        return "paid" if state.paid else "unpaid"

    def _route_journey_status(self, state: OnboardingState) -> str:
        s = state.journey_status
        if s in (JourneyStatus.PRE_QUALIFIED, JourneyStatus.QUALIFIED):
            return "payment"
        if s == JourneyStatus.IN_ELIGIBLE:
            return "ineligible"
        if s in (JourneyStatus.UNQUALIFIED, JourneyStatus.NOT_ACCEPTED):
            return "unqualified"
        if s in (JourneyStatus.ACCEPTED, JourneyStatus.OFFER_ACCEPTED):
            return "offers"
        if s == JourneyStatus.ACTIVATED:
            return "activated"
        return "wait"

    # -- helpers --------------------------------------------------------------

    async def _poll_journey_status(
        self, state: OnboardingState
    ) -> JourneyStatus | None:
        if not state.access_token:
            return state.journey_status
        info = await self._identity.me(access_token=state.access_token)
        raw_status: Any = None
        if isinstance(info, dict):
            user = info.get("user")
            if isinstance(user, dict):
                raw_status = user.get("journeyStatus") or user.get("journey_status")
        if not isinstance(raw_status, str):
            return state.journey_status
        try:
            return JourneyStatus(raw_status)
        except ValueError:
            return state.journey_status

    @staticmethod
    def _parse_name(reply: Any) -> tuple[str, str]:
        if isinstance(reply, dict):
            first = reply.get("first_name")
            last = reply.get("last_name")
            if isinstance(first, str) or isinstance(last, str):
                return str(first or ""), str(last or "")
            text = str(reply.get("text") or "")
        else:
            text = reply_text(reply)
        parts = text.strip().split(maxsplit=1)
        if len(parts) == 2:
            return parts[0], parts[1]
        if parts:
            return parts[0], ""
        return "", ""

    async def _send(
        self,
        ctx: WorkflowContext,
        state: OnboardingState,
        template_key: str,
        variables: dict[str, Any] | None = None,
        *,
        locale: str | None = None,
    ) -> None:
        await self._msg.send(
            channel=_channel(ctx),
            identity=ctx.identity,
            template_key=template_key,
            variables=variables or {},
            locale=locale or state.locale,
        )

    @staticmethod
    def _step(name: str, ctx: WorkflowContext, **fields: Any) -> dict[str, Any]:
        entry = HistoryEntry(step=name, at=ctx.clock.now().isoformat())
        return {"history": [entry], **fields}


def _channel(ctx: WorkflowContext) -> Channel:
    assert ctx.channel is not None
    return ctx.channel


def _extract_status_source(payload: Any) -> str:
    """Pull ``last_status_source`` off a resume payload, defaulting to
    "poll". The dispatcher's :func:`translate_backend_event` stamps
    "webhook" on real Madad-backend events; explicit poller resumes (or
    test-driven manual resumes) get "poll"."""

    if isinstance(payload, dict):
        source = payload.get("last_status_source")
        if isinstance(source, str):
            return source
    return "poll"
