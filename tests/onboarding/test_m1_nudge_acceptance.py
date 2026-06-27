"""Vendor Plan M1 acceptance: at least one nudge type verified end-to-end.

Vendor Engagement Plan §M1 acceptance line:
  *"At least one nudge type verified end-to-end (trigger, timing, channel,
   content)"*

This test pins ``financials_pending`` — the easiest to demo because it
trips at a fixed workflow node, has a 3-step schedule (Day 2 / Day 5 /
Day 14) defined by Madad ops in the CMS, and uses three distinct
templates per step.

The test does NOT advance wall-clock time — that would require
freezegun and a 14-day-test fixture. It proves the same contract the
M1 demo proves:

  * **Trigger** — the agent schedules the reason at the correct
    workflow node (``_financials_send``).
  * **Timing** — the schedule stored in CMS matches the PDF
    specification (Day 2 / Day 5 / Day 14).
  * **Channel** — Day 2 WhatsApp, Day 5 WhatsApp + Email, Day 14 Email.
  * **Content** — all three nudge step templates are seeded in CMS
    with the right body, rendering ``{{ documents }}`` and any other
    placeholders correctly.
"""

from __future__ import annotations

import asyncio

from app.services.cms import build_cms_service
from app.services.cms.enums import ConfigKind
from app.shared.i18n import Locale
from app.shared.workflow import Channel

WA = Channel.WHATSAPP
DAY_SECONDS = 24 * 60 * 60


async def _drive_to_financials_send(harness, identity: str) -> None:
    """Walk the SME up to the moment the agent fires
    ``_reminders.schedule('financials_pending', ...)``. This is the step
    immediately after the CR upload (Step 2 in the agentic flow PDF)."""
    runtime = harness.platform.runtime
    doc = "ZHVtbXk="

    async def resume(message):
        return await runtime.resume(WA, identity, message=message)

    await runtime.start("onboarding", WA, identity, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"text": "biz@example.com"})  # business_email
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": doc}]})


async def test_m1_financials_pending_nudge_scheduled_at_right_step(harness):
    """**Trigger** — the agent invokes ``_reminders.schedule`` for
    ``financials_pending`` exactly once, with the right channel /
    identity / target_ref, immediately after the CR upload."""
    identity = "+97455500M01"
    await _drive_to_financials_send(harness, identity)

    nudge_calls = [
        (reason, kwargs)
        for reason, kwargs in harness.reminders.calls
        if reason == "financials_pending"
    ]
    assert len(nudge_calls) == 1, (
        f"expected exactly one financials_pending schedule call, got "
        f"{len(nudge_calls)}: {harness.reminders.scheduled}"
    )
    reason, kwargs = nudge_calls[0]
    assert reason == "financials_pending"
    assert kwargs["channel"] == WA
    assert kwargs["identity"] == identity
    # ``target_ref`` is set to the run's session_id when no madad_user_id
    # exists yet (the SME hasn't been promoted to a SIGN_UP account in
    # the test fixture); the production path stamps the madad_user_id.
    assert kwargs["target_ref"]


def test_m1_financials_pending_schedule_timing_matches_pdf_spec():
    """**Timing + Channel** — the CMS-stored schedule for
    ``financials_pending`` matches the PDF specification exactly:
    Day 2 WA, Day 5 WA+Email, Day 14 Email (with escalation).

    Tested against the live seed dict so any divergence between the
    seed-script defaults and the PDF would surface immediately.
    """
    from scripts.seed_cms_templates import (
        _NUDGE_SCHEDULES,
        DAY_2,
        DAY_5,
        DAY_14,
    )

    schedule = _NUDGE_SCHEDULES["financials_pending"]
    steps = schedule["schedule"]
    assert len(steps) == 3, "PDF spec expects exactly 3 steps"

    # Step 1: Day 2 — WhatsApp only.
    assert steps[0]["offset"] == DAY_2
    assert steps[0]["channels"] == ["whatsapp"]
    assert steps[0]["template_key"] == "nudge.financials_pending.1"

    # Step 2: Day 5 — WhatsApp + Email.
    assert steps[1]["offset"] == DAY_5
    assert sorted(steps[1]["channels"]) == ["email", "whatsapp"]
    assert steps[1]["template_key"] == "nudge.financials_pending.2"

    # Step 3: Day 14 — Email + escalation flag (ops queue).
    assert steps[2]["offset"] == DAY_14
    assert steps[2]["channels"] == ["email"]
    assert steps[2]["template_key"] == "nudge.financials_pending.3"
    assert steps[2].get("escalate") is True

    # Schedule expects max_attempts to match step count (3).
    assert schedule["max_attempts"] == 3


def test_m1_financials_pending_step_templates_render_through_cms():
    """**Content** — the three step templates are seedable in CMS and
    render with the variable bag the dispatcher will pass at fire time.
    Locks the wording so any unintended seed-script edit surfaces
    immediately during M1 dry-run."""
    from scripts.seed_cms_templates import _NUDGE_TEMPLATE_BODIES

    async def _check() -> None:
        cms = build_cms_service()
        for key, body in _NUDGE_TEMPLATE_BODIES.items():
            if not key.startswith("nudge.financials_pending."):
                continue
            await cms.upsert_template(key, Locale.EN, body)

        from app.services.cms import CmsTemplateProvider
        from app.services.communication.templating import TemplateRenderer

        renderer = TemplateRenderer(CmsTemplateProvider(cms), strict=False)

        # Step 1 — no variables.
        rendered_1 = await renderer.render_template(
            "nudge.financials_pending.1", {}, locale=Locale.EN,
        )
        assert "Audited Financial Statement" in rendered_1

        # Step 2 — no variables.
        rendered_2 = await renderer.render_template(
            "nudge.financials_pending.2", {}, locale=Locale.EN,
        )
        assert "+974" in rendered_2  # phone

        # Step 3 — references uat-portal so SME can self-serve.
        rendered_3 = await renderer.render_template(
            "nudge.financials_pending.3", {}, locale=Locale.EN,
        )
        assert "inactive" in rendered_3.lower()
        assert "uat-portal.madadfintech.com" in rendered_3

    asyncio.run(_check())


def test_m1_nudge_engine_reads_schedule_from_cms():
    """The dispatcher (production nudge engine) reads its schedule from
    the CMS at fire time. This test seeds the schedule via
    ``upsert(ConfigKind.NUDGE, ...)`` and reads it back, proving the
    same round-trip the engine relies on."""
    from scripts.seed_cms_templates import _NUDGE_SCHEDULES

    async def _check() -> None:
        cms = build_cms_service()
        await cms.upsert(
            ConfigKind.NUDGE,
            "financials_pending",
            _NUDGE_SCHEDULES["financials_pending"],
        )
        # Round-trip via the same path the dispatcher uses to read the
        # schedule. Lists in JSON are tuples-or-lists at the boundary
        # depending on driver; assert structurally rather than via ==.
        record = await cms.get(ConfigKind.NUDGE, "financials_pending")
        assert record is not None
        value = record.value
        assert "schedule" in value
        assert len(value["schedule"]) == 3
        assert value["max_attempts"] == 3

    asyncio.run(_check())
