"""Bug #15 (UAT 2026-06-09): the full ✅/⚠️ checklist body fired on every
per-file POST. Madad's bridge fans a multi-doc upload out as N separate
inbounds; the SME got the 15-line checklist body N times in a row
("Application checklist:..." three times within the same minute in the
UAT screenshot).

Fix: debounce the full checklist body via ``state.documents_checklist_sent_at``.
Per upload still fires the brief ✅ receipt so the SME sees progress; the
full body only re-renders once the prior one is older than
``DOCS_CHECKLIST_TTL_SECONDS`` — or whenever the upload completes the
checklist (one final all-✅ snapshot before the coffee message).
"""

from __future__ import annotations

import inspect

from app.services.workflow.onboarding import (
    DOCS_CHECKLIST_TTL_SECONDS,
    OnboardingWorkflow,
)
from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455501501"
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


def _bodies_for_template(harness, key: str) -> list[str]:
    return [
        s["variables"]["results"]
        for s in harness.messenger.sent
        if s["template_key"] == key
    ]


async def test_full_checklist_renders_on_first_upload_only(harness) -> None:
    """Bursting 5 uploads within the debounce window fires the FULL
    body exactly once — on the first inbound. The other 4 receipts
    are brief (no "📋 Application checklist:" header)."""
    runtime = harness.platform.runtime
    await _drive_to_documents(harness)

    filenames = [
        "establishment.pdf",
        "bank_statement.pdf",
        "passport.pdf",
        "interim.pdf",
        "payable.pdf",
    ]
    for fn in filenames:
        await runtime.resume(
            WA, IDENTITY,
            message={"attachments": [{"filename": fn, "content_base64": DOC}]},
        )

    bodies = _bodies_for_template(harness, "onboarding.documents.single_received")
    full_bodies = [b for b in bodies if "📋 Application checklist:" in b]
    assert len(full_bodies) == 1, (
        f"expected exactly 1 full-checklist render across the burst, "
        f"got {len(full_bodies)}"
    )
    # All other receipts are brief — no header.
    brief_bodies = [b for b in bodies if "📋 Application checklist:" not in b]
    assert len(brief_bodies) == len(filenames) - 1


async def test_brief_receipts_still_acknowledge_each_upload(harness) -> None:
    """Even when the full body is debounced, each upload must produce a
    visible brief receipt — silent suppression of the upload would
    re-introduce the Bug #1b silent-drop pattern."""
    runtime = harness.platform.runtime
    await _drive_to_documents(harness)

    for fn in ["establishment.pdf", "bank_statement.pdf", "passport.pdf"]:
        await runtime.resume(
            WA, IDENTITY,
            message={"attachments": [{"filename": fn, "content_base64": DOC}]},
        )

    bodies = _bodies_for_template(harness, "onboarding.documents.single_received")
    # 3 uploads => 3 receipts (one full, two brief).
    assert len(bodies) == 3
    # Each receipt contains the validated doc's name with ✅.
    assert any("✅ Establishment Card — Received & Validated" in b for b in bodies)
    assert any("✅ Bank Statement" in b for b in bodies)
    assert any("✅ Shareholder Passport" in b for b in bodies)


def test_debounce_window_constant_is_reasonable() -> None:
    """Pin the window so a future tweak doesn't accidentally collapse it
    back to zero (re-spamming the SME) or stretch it past a reasonable
    upload-session length."""
    assert 30.0 <= DOCS_CHECKLIST_TTL_SECONDS <= 300.0


async def test_force_full_body_when_only_one_doc_remains(harness) -> None:
    """White-box: when the upload that JUST landed leaves the checklist
    fully exhausted (``still_missing`` empty) the debounce is bypassed
    and the full body fires — so the SME's final upload always carries
    the all-✅ snapshot. The code path's ``or not still_missing`` guard
    is the contract here."""
    source = inspect.getsource(OnboardingWorkflow._acknowledge_uploads)
    # The escape clause that forces the full body on completion.
    assert "or not still_missing" in source, (
        "the docs-complete escape clause was removed — Bug #15 regression"
    )
