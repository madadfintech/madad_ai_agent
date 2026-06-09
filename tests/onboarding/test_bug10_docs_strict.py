"""Bug #10a + #10b (2026-06-09): strict docs gating + classifier honesty.

QA screenshot (Madad Test Number, 3:48 PM): SME uploaded a passport — the
backend classifier returned "Commercial Registration" — and the agent
sent:
  ✅ Commercial Registration — Received & Validated
  🎊 Great — all documents received!
The SME had only sent 1 file; 10+ docs were still missing. Two bugs:

  10a) ``_route_documents`` was lenient — any single upload completed
       the loop. Now strict: stay parked until the asked-for list is
       exhausted OR backend advances the journey past pre-qualification.
  10b) An upload whose classifier-resolved type isn't on the asked-for
       list lands as ⏳ "received, our team will review it" instead of
       ✅ Validated. The SME never sees a false validation again.
"""

from __future__ import annotations

from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455501001"
DOC = "ZHVtbXk="


async def _drive_to_documents(harness) -> None:
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})
    await runtime.resume(
        WA, IDENTITY, message={"attachments": [{"filename": "CR.pdf", "content_base64": DOC}]}
    )
    await runtime.resume(
        WA,
        IDENTITY,
        message={"attachments": [{"filename": "Audited.pdf", "content_base64": DOC}]},
    )
    await runtime.resume(
        WA,
        IDENTITY,
        message={"event": "prequalification.completed", "journey_status": "PRE_QUALIFIED"},
    )


async def test_single_doc_does_not_complete_strict(harness) -> None:
    """Bug #10a: one upload must NOT complete the loop."""
    await _drive_to_documents(harness)
    result = await harness.platform.runtime.resume(
        WA,
        IDENTITY,
        message={
            "attachments": [
                {"filename": "Establishment.pdf", "content_base64": DOC}
            ]
        },
    )

    assert result.prompt == {"waiting_for": "upload", "step": "documents"}
    templates = harness.messenger.templates()
    assert "onboarding.documents.complete" not in templates


async def test_misclassified_doc_lands_as_unprocessed(harness) -> None:
    """Bug #10b: an upload classifier-tagged as something we DIDN'T ask
    for (e.g. backend returns commercial_registration for a passport in
    the post-prequal docs phase) must NOT be marked ✅. Instead, the
    user sees ⏳ 'received, our team will review it'."""
    await _drive_to_documents(harness)

    # The in-memory classifier maps 'cr' filename → commercial_registration,
    # which is NOT on DEFAULT_WHATSAPP_REQUIRED_DOCS (CR was collected back
    # in step 2). So this upload's resolved type isn't on the asked-for list.
    result = await harness.platform.runtime.resume(
        WA,
        IDENTITY,
        message={
            "attachments": [
                {"filename": "Passport_misnamed_as_CR.pdf", "content_base64": DOC}
            ]
        },
    )

    # Loop stays parked (Bug #10a invariant).
    assert result.prompt == {"waiting_for": "upload", "step": "documents"}
    # The single_received template fires (we got SOMETHING), but it carries
    # only the ⏳ row — never the ✅ row that the lenient mode produced.
    sent = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.documents.single_received"
    ]
    assert sent, "single_received template should have fired"
    results_text = sent[-1]["variables"]["results"]
    assert "⏳" in results_text
    assert "✅ Commercial Registration" not in results_text


async def test_additional_document_gets_assigned_to_next_pending_slot(harness) -> None:
    """QA #3 refinement (2026-06-09): when the classifier returns
    ``additional_document`` (truly unknown), don't block the SME — assign
    to the next pending required slot. Madad's team re-buckets if needed.

    Distinct from the misclassification case (passport→CR): a SPECIFIC
    wrong type still lands as ⏳, but a 'don't know' falls through to
    filename inference / next-pending fallback so the SME's checklist
    keeps moving."""
    await _drive_to_documents(harness)

    # IMG-7651.jpg is a filename the keyword classifier won't match
    # (the in-memory classifier returns "additional_document"), AND the
    # filename keywords don't infer a doc type either. Should land in
    # the first pending slot ("national_address_certificate") with ✅.
    await harness.platform.runtime.resume(
        WA,
        IDENTITY,
        message={
            "attachments": [{"filename": "IMG-7651.jpg", "content_base64": DOC}]
        },
    )

    sent = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.documents.single_received"
    ]
    assert sent, "single_received should have fired"
    results_text = sent[-1]["variables"]["results"]
    # The first required slot earns ✅ — not ⏳ — so the SME's progress
    # is reflected and the checklist advances.
    assert "✅" in results_text
    assert "National Address Certificate" in results_text


async def test_forward_status_webhook_exits_strict_loop(harness) -> None:
    """Bug #10a escape hatch: a backend webhook flagging QUALIFIED+ still
    completes the loop without all docs in (admin signed off off-band)."""
    await _drive_to_documents(harness)

    # No upload yet — just admin advancement.
    result = await harness.platform.runtime.resume(
        WA,
        IDENTITY,
        message={"event": "documents.completed", "journey_status": "QUALIFIED"},
    )

    assert "onboarding.documents.complete" in harness.messenger.templates()
    # Run moves on to payment_wait.
    assert result.prompt == {"waiting_for": "payment_ready", "step": "payment_wait"}
