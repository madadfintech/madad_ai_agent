"""Bug #11 (UAT 2026-06-09): bridge-side per-file POST burst.

QA UAT on +91 9497191690: SME uploaded a multi-doc batch. Madad's bridge
POSTed each file as a separate inbound (8 inbounds in ~3 seconds). The
agent:
  1. Fired the "📦 Got it — processing your documents now…" ack for
     every single POST — 8 acks in the SME's chat.
  2. Resumed the same parked run concurrently 8 times, racing on
     ``state.missing_documents`` so two distinct files both claimed the
     same slot (""Interim Financial Statement"" appeared twice in the
     ✅ list).

Fixes:
  * Per-(channel, identity) ``asyncio.Lock`` in the dispatcher serialises
    the concurrent posts so the second waits for the first's
    missing_documents update to commit before reading its own snapshot.
  * 30-second debounce on the processing ack so the SME only sees it
    once per batch.
"""

from __future__ import annotations

import asyncio

from app.services.workflow.dispatcher import OnboardingDispatcher
from app.services.workflow.onboarding import DOCS_PROCESSING_ACK_TTL_SECONDS
from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455501101"
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


async def test_processing_ack_debounced_within_window(harness) -> None:
    """Two doc uploads inside the debounce window fire the processing
    ack ONCE, not twice."""
    await _drive_to_documents(harness)

    # Two back-to-back uploads — well within DOCS_PROCESSING_ACK_TTL_SECONDS.
    await harness.platform.runtime.resume(
        WA,
        IDENTITY,
        message={"attachments": [{"filename": "Establishment.pdf", "content_base64": DOC}]},
    )
    await harness.platform.runtime.resume(
        WA,
        IDENTITY,
        message={"attachments": [{"filename": "QID.pdf", "content_base64": DOC}]},
    )

    proc_count = harness.messenger.templates().count("onboarding.documents.processing")
    assert proc_count == 1, (
        f"expected exactly one processing ack across the burst, got {proc_count}"
    )


async def test_dispatcher_serialises_concurrent_inbounds_per_identity(harness) -> None:
    """Bursting 5 inbound POSTs at the same identity must NOT race the
    parked run. The dispatcher's per-identity lock queues them so each
    sees the previous one's committed state."""
    await _drive_to_documents(harness)
    dispatcher = OnboardingDispatcher(harness.platform.runtime)

    files = [
        ("Establishment_Card.pdf", DOC),
        ("QID.pdf", DOC),
        ("Passport.pdf", DOC),
        ("Bank_Statement.pdf", DOC),
        ("Trade.pdf", DOC),
    ]

    # Fire all five concurrently. The lock should funnel them.
    results = await asyncio.gather(
        *[
            dispatcher.inbound(
                WA,
                IDENTITY,
                attachments=[{"filename": fn, "content_base64": b}],
            )
            for fn, b in files
        ]
    )

    assert all(r is not None for r in results)
    # Run is still parked at documents — strict gate held under burst.
    final = results[-1]
    assert final is not None
    assert final.prompt == {"waiting_for": "upload", "step": "documents"}
    # Every receipt fired exactly once — no duplicate ✅ slot claims.
    receipts = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.documents.single_received"
    ]
    # Tally per-doc-type appearances in the rendered "results" body.
    appearances: dict[str, int] = {}
    for r in receipts:
        for line in r["variables"]["results"].splitlines():
            if "—" in line and "✅" in line:
                label = line.split("—")[0].strip().lstrip("✅").strip()
                appearances[label] = appearances.get(label, 0) + 1
    # No doc type validated more than once across the whole burst —
    # which was the duplicate-Interim-Financial-Statement bug.
    for doc_label, count in appearances.items():
        assert count == 1, f"{doc_label!r} validated {count} times — concurrency race"


def test_debounce_window_constant_is_reasonable() -> None:
    """Pin the debounce window so a future tweak doesn't accidentally
    collapse it back to 0 (and undo the fix) or stretch it so far that
    a genuinely-distinct re-batch is silently swallowed."""
    assert 10.0 <= DOCS_PROCESSING_ACK_TTL_SECONDS <= 120.0
