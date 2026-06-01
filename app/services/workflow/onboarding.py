"""Phase 1.a onboarding workflow (Steps 1–8) — a deterministic LangGraph graph.

Runs on the shared workflow runtime (retry, recovery, session continuity are
inherited) and orchestrates the platform through injected ports (Messenger,
DocumentIntake, MadadClient, PaymentClient, Reminders). All conversational copy
is rendered from CMS templates (multilingual via ``locale``); no external system
is touched directly — only through ports (MCP-backed).

Node structure: each interaction is an **action node** (side-effects: send a
prompt / call Madad / create a payment link / schedule a nudge — runs exactly
once) followed by a pure **await node** (``interrupt()`` first, then process the
reply). This split is required because LangGraph re-runs the code *before* an
``interrupt`` on resume; keeping side-effects in non-interrupting nodes prevents
duplicate sends/calls. Two resume sources: user replies (inbound messages) and
external decisions (Madad/Tess webhooks).
"""

from __future__ import annotations

from typing import Any

from app.shared.workflow import GraphBuilder, WorkflowContext, WorkflowDefinition, await_input
from app.shared.workflow.state import HistoryEntry

from .ports import DocumentIntake, MadadClient, Messenger, PaymentClient, Reminders
from .state import OnboardingState, is_yes, reply_attachments

CHECKLIST_NAME = "onboarding"
ONBOARDING_FEE_QAR = 6000

# Template keys this workflow renders (seed these in the CMS).
TEMPLATE_KEYS = [
    "onboarding.campaign.intro",
    "onboarding.declined",
    "onboarding.consent.request",
    "onboarding.not_eligible",
    "onboarding.financials.request",
    "onboarding.prequal.pending",
    "onboarding.not_prequalified",
    "onboarding.checklist.request",
    "onboarding.documents.missing",
    "onboarding.documents.complete",
    "onboarding.not_qualified",
    "onboarding.payment.request",
    "onboarding.submission.confirmed",
    "onboarding.offers.preview",
    "onboarding.creditline.active",
]


class OnboardingWorkflow(WorkflowDefinition):
    name = "onboarding"
    version = 1
    state_schema = OnboardingState

    def __init__(
        self,
        *,
        messenger: Messenger,
        documents: DocumentIntake,
        madad: MadadClient,
        payments: PaymentClient,
        reminders: Reminders,
    ) -> None:
        self._msg = messenger
        self._docs = documents
        self._madad = madad
        self._pay = payments
        self._reminders = reminders

    # -- graph wiring ---------------------------------------------------------

    def build(self, graph: GraphBuilder) -> None:
        nodes = {
            "campaign_send": self._campaign_send,
            "campaign_await": self._campaign_await,
            "declined": self._declined,
            "consent_send": self._consent_send,
            "consent_await": self._consent_await,
            "eligibility": self._eligibility,
            "not_eligible": self._not_eligible,
            "financials_send": self._financials_send,
            "financials_await": self._financials_await,
            "prequal_trigger": self._prequal_trigger,
            "prequal_await": self._prequal_await,
            "not_prequalified": self._not_prequalified,
            "checklist": self._checklist,
            "collect_await": self._collect_await,
            "documents_missing": self._documents_missing,
            "documents_complete": self._documents_complete,
            "risk_trigger": self._risk_trigger,
            "risk_await": self._risk_await,
            "not_qualified": self._not_qualified,
            "payment_send": self._payment_send,
            "payment_await": self._payment_await,
            "bank_submission": self._bank_submission,
            "lender_await": self._lender_await,
            "offer_send": self._offer_send,
            "offer_await": self._offer_await,
            "credit_line": self._credit_line,
        }
        for node_name, fn in nodes.items():
            graph.add_node(node_name, fn)

        graph.set_entry("campaign_send")
        graph.add_edge("campaign_send", "campaign_await")
        graph.add_conditional_edges(
            "campaign_await", self._route_entry, {"consent": "consent_send", "declined": "declined"}
        )
        graph.add_edge("consent_send", "consent_await")
        graph.add_edge("consent_await", "eligibility")
        graph.add_conditional_edges(
            "eligibility", self._route_eligibility, {"ok": "financials_send", "no": "not_eligible"}
        )
        graph.add_edge("financials_send", "financials_await")
        graph.add_edge("financials_await", "prequal_trigger")
        graph.add_edge("prequal_trigger", "prequal_await")
        graph.add_conditional_edges(
            "prequal_await", self._route_prequal, {"ok": "checklist", "no": "not_prequalified"}
        )
        graph.add_edge("checklist", "collect_await")
        graph.add_conditional_edges(
            "collect_await",
            self._route_documents,
            {"complete": "documents_complete", "missing": "documents_missing"},
        )
        graph.add_edge("documents_missing", "collect_await")
        graph.add_edge("documents_complete", "risk_trigger")
        graph.add_edge("risk_trigger", "risk_await")
        graph.add_conditional_edges(
            "risk_await", self._route_risk, {"ok": "payment_send", "no": "not_qualified"}
        )
        graph.add_edge("payment_send", "payment_await")
        graph.add_conditional_edges(
            "payment_await",
            self._route_payment,
            {"paid": "bank_submission", "unpaid": "payment_send"},
        )
        graph.add_edge("bank_submission", "lender_await")
        graph.add_edge("lender_await", "offer_send")
        graph.add_edge("offer_send", "offer_await")
        graph.add_edge("offer_await", "credit_line")

        for terminal in (
            "declined",
            "not_eligible",
            "not_prequalified",
            "not_qualified",
            "credit_line",
        ):
            graph.set_finish(terminal)

    # -- Step 1: campaign entry ----------------------------------------------

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

    # -- Step 2: consent + CR -------------------------------------------------

    async def _consent_send(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.consent.request")
        return self._step("consent_send", ctx)

    async def _consent_await(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        reply = await_input({"waiting_for": "upload", "step": "consent_cr"})
        attachments = reply_attachments(reply)
        cr_ref = attachments[0]["filename"] if attachments else None
        if cr_ref:
            await self._docs.ingest(application_ref=state.application_ref, filename=cr_ref)
        return self._step("consent_await", ctx, consent=True, cr_ref=cr_ref)

    # -- Step 3: eligibility --------------------------------------------------

    async def _eligibility(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        eligible = await self._madad.check_eligibility(state.cr_ref)
        return self._step("eligibility", ctx, eligible=eligible)

    async def _not_eligible(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.not_eligible")
        return self._step("not_eligible", ctx, outcome="not_eligible")

    # -- Step 4: financial report + pre-qualification ------------------------

    async def _financials_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.financials.request")
        await self._reminders.schedule(
            "financials_pending",
            channel=_channel(ctx),
            identity=ctx.identity,
            target_ref=state.application_ref or ctx.session_id,
        )
        return self._step("financials_send", ctx)

    async def _financials_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        reply = await_input({"waiting_for": "upload", "step": "financials"})
        attachments = reply_attachments(reply)
        if attachments:
            await self._docs.ingest(
                application_ref=state.application_ref, filename=attachments[0]["filename"]
            )
        await self._reminders.suppress(target_ref=state.application_ref or ctx.session_id)
        return self._step("financials_await", ctx, financials_received=True)

    async def _prequal_trigger(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        ref = await self._madad.request_prequalification(identity=ctx.identity, cr_ref=state.cr_ref)
        await self._send(ctx, state, "onboarding.prequal.pending", {"ref": ref})
        return self._step("prequal_trigger", ctx, application_ref=ref)

    async def _prequal_await(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        result = await_input({"waiting_for": "prequalification", "step": "prequalification"})
        qualified = bool(result.get("qualified")) if isinstance(result, dict) else False
        return self._step("prequal_await", ctx, prequalified=qualified)

    async def _not_prequalified(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.not_prequalified")
        return self._step("not_prequalified", ctx, outcome="not_prequalified")

    # -- Step 5–6: dynamic checklist + document collection -------------------

    async def _checklist(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        missing = await self._docs.missing(
            checklist=CHECKLIST_NAME, application_ref=state.application_ref
        )
        await self._send(
            ctx, state, "onboarding.checklist.request", {"documents": ", ".join(missing)}
        )
        await self._reminders.schedule(
            "incomplete_docs",
            channel=_channel(ctx),
            identity=ctx.identity,
            target_ref=state.application_ref,
        )
        return self._step("checklist", ctx, missing_documents=missing)

    async def _collect_await(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        reply = await_input({"waiting_for": "upload", "step": "documents"})
        for attachment in reply_attachments(reply):
            await self._docs.ingest(
                application_ref=state.application_ref, filename=attachment["filename"]
            )
        missing = await self._docs.missing(
            checklist=CHECKLIST_NAME, application_ref=state.application_ref
        )
        if not missing:
            await self._reminders.suppress(target_ref=state.application_ref)
        return self._step("collect_await", ctx, missing_documents=missing)

    async def _documents_missing(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(
            ctx,
            state,
            "onboarding.documents.missing",
            {"documents": ", ".join(state.missing_documents)},
        )
        return self._step("documents_missing", ctx)

    async def _documents_complete(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.documents.complete")
        return self._step("documents_complete", ctx)

    # -- Step 7: risk score ---------------------------------------------------

    async def _risk_trigger(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        await self._madad.request_score(state.application_ref)
        return self._step("risk_trigger", ctx)

    async def _risk_await(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        result = await_input({"waiting_for": "score", "step": "risk_assessment"})
        data = result if isinstance(result, dict) else {}
        return self._step(
            "risk_await", ctx, score=data.get("score"), risk_qualified=bool(data.get("qualified"))
        )

    async def _not_qualified(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.not_qualified")
        return self._step("not_qualified", ctx, outcome="not_qualified")

    # -- Step 8: payment, submission, offers, credit line --------------------

    async def _payment_send(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        link = await self._pay.create_link(
            application_ref=state.application_ref, amount=ONBOARDING_FEE_QAR
        )
        await self._send(
            ctx,
            state,
            "onboarding.payment.request",
            {"score": state.score, "amount": f"{ONBOARDING_FEE_QAR:,}", "link": link},
        )
        await self._reminders.schedule(
            "payment_pending",
            channel=_channel(ctx),
            identity=ctx.identity,
            target_ref=state.application_ref,
        )
        return self._step("payment_send", ctx)

    async def _payment_await(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        result = await_input({"waiting_for": "payment", "step": "payment"})
        paid = bool(result.get("paid")) if isinstance(result, dict) else False
        if paid:
            await self._reminders.suppress(target_ref=state.application_ref)
        return self._step("payment_await", ctx, paid=paid)

    async def _bank_submission(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._madad.submit_to_lenders(state.application_ref)
        await self._send(ctx, state, "onboarding.submission.confirmed")
        return self._step("bank_submission", ctx)

    async def _lender_await(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        result = await_input({"waiting_for": "offers", "step": "lender_evaluation"})
        offers = list(result.get("offers", [])) if isinstance(result, dict) else []
        return self._step("lender_await", ctx, offers=offers)

    async def _offer_send(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.offers.preview", {"count": len(state.offers)})
        return self._step("offer_send", ctx)

    async def _offer_await(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        result = await_input({"waiting_for": "offer_selection", "step": "offer_preview"})
        offer_id = result.get("offer_id") if isinstance(result, dict) else None
        selected = next(
            (o for o in state.offers if o.get("offer_id") == offer_id),
            state.offers[0] if state.offers else None,
        )
        return self._step("offer_await", ctx, selected_offer=selected)

    async def _credit_line(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        await self._madad.activate_credit_line(state.application_ref, state.selected_offer or {})
        await self._send(ctx, state, "onboarding.creditline.active")
        return self._step("credit_line", ctx, credit_line_active=True, outcome="completed")

    # -- routers --------------------------------------------------------------

    def _route_entry(self, state: OnboardingState) -> str:
        return "consent" if state.entry_reply == "YES" else "declined"

    def _route_eligibility(self, state: OnboardingState) -> str:
        return "ok" if state.eligible else "no"

    def _route_prequal(self, state: OnboardingState) -> str:
        return "ok" if state.prequalified else "no"

    def _route_documents(self, state: OnboardingState) -> str:
        return "missing" if state.missing_documents else "complete"

    def _route_risk(self, state: OnboardingState) -> str:
        return "ok" if state.risk_qualified else "no"

    def _route_payment(self, state: OnboardingState) -> str:
        return "paid" if state.paid else "unpaid"

    # -- helpers --------------------------------------------------------------

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


def _channel(ctx: WorkflowContext) -> Any:
    assert ctx.channel is not None
    return ctx.channel
