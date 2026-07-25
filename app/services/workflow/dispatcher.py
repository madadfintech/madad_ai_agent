"""Dispatcher — bridges inbound channel messages and webhooks to the runtime.

* :meth:`on_inbound` implements the Communication service's ``ConversationDispatcher``
  seam: the first message starts the onboarding workflow (campaign entry /
  organic contact); later messages resume the waiting run (reconnect recovery
  is free).
* :meth:`resume_external` resumes a run waiting on an external decision from
  a raw payload (test / internal-API entry).
* :meth:`on_backend_event` is the single chokepoint for backend webhook
  events: validates the event type, dedupes on ``event_id``, translates the
  payload into the workflow's resume shape, and resumes. This is the only
  entry route that production webhook receivers should call.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

from app.shared.workflow import Channel, ExecutionResult, WorkflowRuntime
from app.shared.workflow.enums import TERMINAL_STATUSES, RunStatus

from .ports import MadadIdentityClient
from .webhook_dedupe import InMemoryWebhookDedupe, WebhookDedupe


def _canon_whatsapp_identity(identity: str) -> str:
    """Canonicalise a phone into the same E.164 form the inbound bridge keys
    WhatsApp sessions by, so an email→phone resolution matches the SME's live
    WhatsApp session key. Mirrors ``main._canon_event_identity`` (Qatar local
    ``66563022`` → ``+97466563022``); duplicated here to avoid a dispatcher↔main
    import cycle."""

    if not identity:
        return identity
    digits = "".join(c for c in identity if c.isdigit())
    if not digits:
        return identity
    if len(digits) == 8:  # Qatar local number -> prepend country code
        digits = "974" + digits
    return "+" + digits

DEFAULT_WORKFLOW = "onboarding"

# QA #2 (2026-06-09): runs that died from a transient failure (network
# blip, backend 403, MCP timeout) should be revived on the next user
# message instead of starting from scratch. The LangGraph checkpoint
# still holds the last parked-step state — we just need to re-transition
# the run to RUNNING and let the executor replay the new inbound at the
# pending interrupt. COMPLETED / CANCELLED / DEAD_LETTERED are intentionally
# NOT in this set (the first is success, the other two are operator-set
# kills that should not silently un-do themselves).
REVIVABLE_TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.FAILED, RunStatus.TIMED_OUT}
)


# -- Canonical backend event types -------------------------------------------
#
# The Phase 4 webhook receivers accept any event type in this combined set.
# Phase 1.a events drive the active onboarding flow; Phase 1.b events are
# registered NOW (so the receiver path can already accept them) but their
# workflow handlers don't land until Phase 6 (invoice financing). Per
# [[project_mcp_catalog]] § "WEBHOOK EVENTS".

PHASE1A_BACKEND_EVENTS: frozenset[str] = frozenset(
    {
        "eligibility.updated",
        "documents.completed",
        "prequalification.completed",
        # UAT 2026-06-17: pre-qualification REJECTION path. Without this
        # the SME parked at ``prequalify_wait_await`` forever when the
        # admin marked the business as not pre-qualified. Translated to
        # ``{type: status_update, prequalification_rejected: True}`` so
        # ``_prequalify_wait_await`` can route to the terminal node.
        "prequalification.rejected",
        "madad_score.ready",
        "payment.completed",
        # UAT 2026-06-16 (afternoon): backend fires ``qualified.waived``
        # when ops "waive off" the onboarding fee. Per Madad, treat it
        # as functionally equivalent to ``payment.completed`` — the fee
        # is satisfied — and advance the run into the lender phase.
        # Backend sends its OWN waiver WhatsApp message, so the agent
        # must stay silent here (only state advances).
        "qualified.waived",
        "offers.available",
        # UAT 2026-06-16 Bug #8: backend fires ``offer.selected`` at
        # acceptance time per Ishan's confirmation, but the agent only
        # accepted ``offer.accepted``. Result: 400 from the webhook
        # chokepoint, ✅ confirmation only landed via the activated
        # backfill block. Accept BOTH names so neither side has to wait
        # on the other to rename.
        "offer.accepted",
        "offer.selected",
        "credit_line.activated",
    }
)

PHASE1B_BACKEND_EVENTS: frozenset[str] = frozenset(
    {
        "transaction.disbursed",
        "repayment.received",
        "repayment.partially_paid",
        "repayment.closed",
        "repayment.due_soon",
        "repayment.overdue",
    }
)

ALL_BACKEND_EVENTS: frozenset[str] = PHASE1A_BACKEND_EVENTS | PHASE1B_BACKEND_EVENTS


# Map well-known event types to the journey_status they imply. The workflow
# uses this hint to advance immediately without an extra auth_me round-trip
# — useful in staging where the operator-issued event is the truth, and
# essential for demos where the test account's backend state doesn't change
# between webhook posts.
EVENT_TO_JOURNEY_STATUS: dict[str, str] = {
    "eligibility.updated": "PRE_QUALIFIED",
    "documents.completed": "PRE_QUALIFIED",
    "prequalification.completed": "PRE_QUALIFIED",
    "madad_score.ready": "QUALIFIED",
    "offers.available": "ACCEPTED",
    # Both names map to the same OFFER_ACCEPTED journey state — see
    # PHASE1A_BACKEND_EVENTS note about Bug #8.
    "offer.accepted": "OFFER_ACCEPTED",
    "offer.selected": "OFFER_ACCEPTED",
    "credit_line.activated": "ACTIVATED",
}


def translate_backend_event(
    event_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Translate a backend event into the workflow's resume payload shape.

    ``payment.completed`` resumes ``payment_await`` with ``paid=True`` so
    ``_route_payment`` advances to ``lender_status_poll``. Status-update
    events resume ``journey_wait_await`` / ``lender_wait_await``;
    well-known events also carry the implied ``journey_status`` so the
    workflow can advance without a separate auth_me round-trip.
    ``last_status_source="webhook"`` rides along so the polling worker
    can suppress its next cycle for this run.

    Phase 1.b events (``transaction.disbursed``, ``repayment.*``) get the
    distinguishing ``type: "phase1b_event"`` marker so the
    ``_invoice_collect_await`` node can route them to the right handler
    without scraping every status_update payload looking for the event
    name.
    """

    base: dict[str, Any] = {"last_status_source": "webhook"}
    if event_type == "prequalification.rejected":
        # Tag the resume payload so ``_prequalify_wait_await`` routes
        # to the not-pre-qualified terminal instead of staying parked.
        return {
            "type": "status_update", "event": event_type,
            "prequalification_rejected": True,
            **base, **payload,
        }
    if event_type in {"payment.completed", "qualified.waived"}:
        # Both produce the same effect: paid=True so ``_route_payment``
        # (and the new ``_route_payment_wait`` lender-jump) advance the
        # run into the lender phase. The wait-node handlers detect the
        # ``qualified.waived`` event marker on the payload and stay
        # silent — backend already sent the SME the waiver message.
        return {
            "type": "payment", "paid": True, "event": event_type,
            **base, **payload,
        }
    if event_type in PHASE1B_BACKEND_EVENTS:
        return {"type": "phase1b_event", "event": event_type, **base, **payload}
    if event_type in EVENT_TO_JOURNEY_STATUS:
        base["journey_status"] = EVENT_TO_JOURNEY_STATUS[event_type]
    return {"type": "status_update", "event": event_type, **base, **payload}


class OnboardingDispatcher:
    """Routes inbound messages and webhooks into the onboarding workflow."""

    def __init__(
        self,
        runtime: WorkflowRuntime,
        *,
        workflow: str = DEFAULT_WORKFLOW,
        dedupe: WebhookDedupe | None = None,
        allowed_event_types: frozenset[str] | set[str] | None = None,
        identity: MadadIdentityClient | None = None,
    ) -> None:
        self._runtime = runtime
        self._workflow = workflow
        self._dedupe = dedupe or InMemoryWebhookDedupe()
        # Cross-channel unify (2026-07-26): read-only registration lookups to map
        # an inbound on one channel to the SME's single canonical run on the
        # OTHER channel. Optional — when None (older wiring / some tests) the
        # dispatcher behaves EXACTLY as before (no cross-channel resolution).
        self._identity = identity
        self._allowed = frozenset(
            allowed_event_types if allowed_event_types is not None else ALL_BACKEND_EVENTS
        )
        # Bug #11 (UAT 2026-06-09): Madad's bridge POSTs each ZIP-member /
        # multi-file upload as a SEPARATE inbound, sometimes within a few
        # hundred milliseconds. The workflow's run was getting hit by
        # several concurrent resumes that all transitioned the run
        # ``running→running`` and raced on ``state.missing_documents``
        # (each saw a stale snapshot, claimed the same slot, fired its
        # own "processing now" ack — user got 8 acks + duplicate ✅
        # entries). Per-(channel, identity) asyncio.Lock serialises the
        # concurrent posts so they queue cleanly instead of racing.
        #
        # OrderedDict + size cap: the previous unbounded dict grew one
        # entry per unique (channel, identity) tuple and never freed
        # them. At scale that's a slow memory leak. The LRU trim evicts
        # the oldest entries whenever the map crosses the cap; an
        # evicted lock is only ever swapped for a fresh one on the next
        # touch — safe because the previous one couldn't have been held
        # (we hold the lock for the duration of one dispatch, and a
        # fresh dispatch for the same identity always touches the map
        # before acquiring the lock).
        self._locks: OrderedDict[tuple[Channel, str], asyncio.Lock] = OrderedDict()
        self._locks_guard = asyncio.Lock()
        self._locks_max = 2048

    async def _identity_lock(
        self, channel: Channel, identity: str
    ) -> asyncio.Lock:
        key = (channel, identity)
        # Two-level guarded creation so two concurrent callers don't race
        # to install distinct locks for the same key.
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
                # Trim the oldest entries when over the cap.
                while len(self._locks) > self._locks_max:
                    self._locks.popitem(last=False)
            else:
                self._locks.move_to_end(key)
        return lock

    @property
    def allowed_event_types(self) -> frozenset[str]:
        return self._allowed

    async def on_inbound(self, message: Any) -> ExecutionResult | None:
        """Start or resume the workflow for an inbound channel message.

        ``message`` is a communication ``Message`` (duck-typed: channel,
        identity, text, attachments). Returns None when a non-None
        ``message_id`` is rejected by the dedupe layer (caller responds 200
        so the source bridge does not retry).
        """

        channel = message.channel
        identity = message.identity
        message_id = getattr(message, "message_id", None)
        payload = {
            "text": message.text,
            "attachments": [
                {"filename": a.filename, "provider_ref": a.provider_ref}
                for a in getattr(message, "attachments", [])
            ],
            "message_id": message_id,
        }
        return await self._dispatch(channel, identity, payload, message_id=message_id)

    async def inbound(
        self,
        channel: Channel,
        identity: str,
        *,
        text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        data: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> ExecutionResult | None:
        """Start/resume from a normalized inbound message (API/testing entry).

        ``data`` carries an arbitrary structured payload (eligibility form,
        buyer / shareholder capture); its keys are merged into the resume
        payload at the top level so the workflow's form-await nodes see
        them directly.

        ``message_id`` is the source bridge's unique id for the inbound
        message (Meta's ``wamid``, SendGrid's ``Message-ID``, etc.). When
        supplied, the call is deduplicated through the shared dedupe layer
        with the prefix ``inbound:`` so the same Meta payload re-delivered
        on a network retry is not re-played through the workflow. Returns
        None on duplicate.
        """

        payload: dict[str, Any] = {"text": text, "attachments": attachments or []}
        if data:
            payload.update(data)
        return await self._dispatch(channel, identity, payload, message_id=message_id)

    async def resume_external(
        self, channel: Channel, identity: str, payload: dict[str, Any]
    ) -> ExecutionResult:
        """Resume a run waiting on an external (webhook) decision."""

        return await self._runtime.resume(channel, identity, message=payload)

    async def on_backend_event(
        self,
        *,
        event_type: str,
        event_id: str | None,
        channel: Channel,
        identity: str,
        payload: dict[str, Any],
    ) -> ExecutionResult | None:
        """Single chokepoint for backend webhook events.

        Returns ``None`` if the event was a duplicate (the dedupe rejected the
        id) — production receivers should respond 200 either way so the
        backend doesn't retry. ``UnknownEventTypeError`` is raised for an
        unknown event type so the receiver can respond with 400.
        """

        if event_type not in self._allowed:
            raise UnknownEventTypeError(event_type)
        if event_id is not None and not await self._dedupe.claim(event_id):
            return None
        resume_payload = translate_backend_event(event_type, payload)
        # [TEMP-DBG] To find exact bug - Temp Logs
        from app.core.logging import get_logger as _dbg_get_logger
        _dbg_get_logger("workflow.dispatcher").info(
            "[TEMP-DBG] dispatcher.event_translated",
            event_type=event_type,
            event_id=event_id,
            channel=str(channel),
            identity=identity,
            resume_type=resume_payload.get("type"),
            resume_event=resume_payload.get("event"),
            resume_paid=resume_payload.get("paid"),
            resume_journey_status=resume_payload.get("journey_status"),
            resume_keys=sorted(resume_payload.keys()),
        )
        return await self.resume_external(channel, identity, resume_payload)

    async def _dispatch(
        self,
        channel: Channel,
        identity: str,
        payload: dict[str, Any],
        *,
        message_id: str | None = None,
    ) -> ExecutionResult | None:
        if message_id is not None and not await self._dedupe.claim(f"inbound:{message_id}"):
            return None
        # Bug #11 (UAT 2026-06-09): hold the per-identity lock around the
        # whole dispatch so the bridge's per-file POST burst can't drive
        # multiple concurrent resumes against the same parked run. The
        # second post waits for the first to finish (state.missing_documents
        # update committed) before reading its own snapshot.
        lock = await self._identity_lock(channel, identity)
        async with lock:
            return await self._dispatch_locked(
                channel, identity, payload, message_id=message_id
            )

    async def _resolve_canonical_run(
        self, channel: Channel, identity: str
    ) -> tuple[Channel, str] | None:
        """Cross-channel unify (2026-07-26). This inbound arrived on a channel
        that has NO usable run of its own. If it belongs to an SME whose SINGLE
        canonical run is live on the OTHER channel (they switched WhatsApp↔email),
        return that run's ``(channel, identity)`` so the caller resumes THAT run —
        replying back on THIS inbound's channel via ``io_channel``/``io_identity``
        — instead of forking a second, drifting run.

        Deliberately conservative: returns ``None`` (⇒ today's exact fresh-start
        path) whenever ANYTHING is uncertain — no identity client, not registered,
        no complementary phone/email on file, no active run on the other channel,
        or any error. That conservatism is precisely what keeps the WhatsApp-only
        real SME and every test member on their existing, unchanged path (a
        WhatsApp inbound with a live WhatsApp run never even reaches here — the
        same-channel branch in ``_dispatch_locked`` returns first).
        """

        if self._identity is None:
            return None
        try:
            reg = await self._identity.check_registration(
                identifier=identity, channel=channel
            )
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(reg, dict) or not reg.get("registered"):
            return None

        # The complementary channel this SME could already be running on.
        if channel is Channel.EMAIL:
            phone = reg.get("phoneNumber")
            if not phone:
                return None
            other_channel, other_identity = (
                Channel.WHATSAPP,
                _canon_whatsapp_identity(str(phone)),
            )
        elif channel is Channel.WHATSAPP:
            email = reg.get("email")
            if not email:
                return None
            other_channel, other_identity = Channel.EMAIL, str(email)
        else:
            return None

        # Never resolve to this same inbound (defensive against a phone that
        # canonicalises back to its own identity).
        if other_channel is channel and other_identity == identity:
            return None

        session = await self._runtime.sessions.get(other_channel, other_identity)
        if session is None or not session.active_run_id:
            return None
        run = await self._runtime.run_store.get_or_none(session.active_run_id)
        if run is None or run.status in TERMINAL_STATUSES:
            return None
        return other_channel, other_identity

    async def _dispatch_locked(
        self,
        channel: Channel,
        identity: str,
        payload: dict[str, Any],
        *,
        message_id: str | None = None,
    ) -> ExecutionResult | None:
        session = await self._runtime.sessions.get(channel, identity)
        if session is not None and session.active_run_id:
            run = await self._runtime.run_store.get_or_none(session.active_run_id)
            if run is not None:
                if run.status not in TERMINAL_STATUSES:
                    return await self._runtime.resume(channel, identity, message=payload)
                if run.status in REVIVABLE_TERMINAL_STATUSES:
                    # QA #2: a transient failure left this run terminally
                    # dead in the store, but the LangGraph checkpoint at
                    # the last successful interrupt is still intact. Flip
                    # the run record back to RUNNING and replay the new
                    # inbound — if the resume itself errors (genuinely
                    # broken state), fall through to a fresh start so the
                    # SME is never stuck.
                    try:
                        await self._runtime.revive_failed_run(run)
                        return await self._runtime.resume(
                            channel, identity, message=payload
                        )
                    except Exception:  # noqa: BLE001
                        pass

        # Cross-channel unify (2026-07-26). No usable run keyed on THIS inbound's
        # own (channel, identity). Before forking a fresh run, check whether the
        # SME already has their single canonical run live on the OTHER channel
        # (they switched WhatsApp↔email). If so, resume THAT run and reply back on
        # THIS channel via io_channel/io_identity — one SME, one state, two
        # channels. `_resolve_canonical_run` is fully conservative: any
        # uncertainty returns None and we fall through to today's fresh start.
        canonical = await self._resolve_canonical_run(channel, identity)
        if canonical is not None:
            canon_channel, canon_identity = canonical
            # The per-identity lock we already hold (from `_dispatch`) is keyed on
            # THIS inbound's channel; take the CANONICAL run's own lock too so a
            # near-simultaneous send on the other channel can't drive two
            # concurrent resumes against the same parked run (Bug #11). The two
            # keys always differ (guarded in `_resolve_canonical_run`), so this
            # never re-enters the lock we hold.
            canon_lock = await self._identity_lock(canon_channel, canon_identity)
            from app.core.logging import get_logger

            try:
                async with canon_lock:
                    get_logger(__name__).info(
                        "cross_channel.unify_resume",
                        inbound_channel=str(channel),
                        inbound_identity=identity,
                        canonical_channel=str(canon_channel),
                        canonical_identity=canon_identity,
                    )
                    return await self._runtime.resume(
                        canon_channel,
                        canon_identity,
                        message=payload,
                        io_channel=channel,
                        io_identity=identity,
                    )
            except Exception as exc:  # noqa: BLE001
                # TOCTOU (mirrors the revivable-run branch above): between
                # `_resolve_canonical_run` confirming the canonical run was live
                # and this resume, a concurrent same-channel resume could have
                # completed/failed it, or the session could have been reset —
                # `resume` then raises (SessionNotFound / already-terminal).
                # Degrade to today's fresh-start path below instead of erroring
                # the inbound, so the SME is never left stuck.
                get_logger(__name__).warning(
                    "cross_channel.unify_resume_failed_fallthrough",
                    inbound_channel=str(channel),
                    inbound_identity=identity,
                    canonical_channel=str(canon_channel),
                    error=str(exc)[:200],
                )

        result = await self._runtime.start(
            self._workflow, channel, identity, input=payload
        )
        # Cross-channel document continuation (2026-07-25). When the FIRST inbound
        # on this channel CREATES the run and it parks ASKING for a document upload,
        # but that same inbound ALREADY carried the document — e.g. a WhatsApp lead
        # who gets frustrated, switches to email and sends their CR / audited
        # financials / checklist in that first email — the run-creation flow would
        # otherwise route to the step, send the "please upload X" prompt and drop
        # the attachment. Replay the inbound ONCE as a resume so the parked upload
        # step ingests the document (identically to how an already-waiting run would
        # have processed it), landing the SME on the correct next step.
        #
        # Guarded tightly: only a fresh start (never a resume) that (a) is waiting,
        # (b) actually carried attachments, and (c) parked on an "upload" prompt.
        # A plain YES / text-only channel switch has no attachments → untouched;
        # WhatsApp's first message is the YES/NO campaign ("reply" prompt) → untouched.
        if (
            result is not None
            and result.waiting
            and bool(payload.get("attachments"))
            and isinstance(result.prompt, dict)
            and result.prompt.get("waiting_for") == "upload"
        ):
            from app.core.logging import get_logger

            get_logger(__name__).info(
                "cross_channel.doc_replay_on_switch",
                channel=str(channel),
                identity=identity,
                step=result.prompt.get("step"),
                attachment_count=len(payload.get("attachments") or []),
            )
            return await self._runtime.resume(channel, identity, message=payload)
        return result


class UnknownEventTypeError(Exception):
    """Raised by :meth:`OnboardingDispatcher.on_backend_event` when the
    backend posts an event type the dispatcher's allowed set doesn't
    recognise."""

    def __init__(self, event_type: str) -> None:
        super().__init__(f"unknown backend event type: {event_type!r}")
        self.event_type = event_type
