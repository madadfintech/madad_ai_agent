"""A9 — payment-link + missing-docs threaded through nudge variables so
each scheduled reminder renders the live state at dispatch time.
"""

from __future__ import annotations

from app.shared.workflow import Channel

WA = Channel.WHATSAPP


async def _drive_to_payment_send(harness, identity: str):
    runtime = harness.platform.runtime
    doc = "ZHVtbXk="

    async def resume(message):
        return await runtime.resume(WA, identity, message=message)

    await runtime.start("onboarding", WA, identity, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": doc}]})
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": doc}]})
    await resume({"event": "prequalification.completed", "madadScore": 78})
    await resume(
        {
            "attachments": [
                {"filename": "Trade_License.pdf", "content_base64": doc},
                {"filename": "Tax_Card.pdf", "content_base64": doc},
            ]
        }
    )
    harness.identity.journey_status = "PRE_QUALIFIED"
    return await resume(
        {"event": "madad_score.ready", "journey_status": "PRE_QUALIFIED"}
    )


async def test_payment_pending_nudge_carries_link_and_amount(harness) -> None:
    """When _payment_send_link schedules the payment_pending reminder, it
    threads {payment_link, amount} as variables so each scheduled step
    renders the LIVE link (per PDF Step 5 nudge spec: 'Payment link
    re-sent with each nudge')."""
    await _drive_to_payment_send(harness, "+97455500A9P")

    payment_calls = [
        kwargs
        for reason, kwargs in harness.reminders.calls
        if reason == "payment_pending"
    ]
    assert len(payment_calls) >= 1
    variables = payment_calls[0].get("variables") or {}
    assert "amount" in variables
    assert "payment_link" in variables
    # The InMemory payment client mints a synthetic link the workflow stashes
    # to state.payment_link; verify it's actually there (not the empty string).
    assert variables["payment_link"].startswith("https://")


async def test_incomplete_docs_nudge_carries_missing_documents(harness) -> None:
    """incomplete_docs nudge threads {documents} so the missing-list shows
    up in each scheduled reminder."""
    runtime = harness.platform.runtime
    identity = "+97455500A9D"
    doc = "ZHVtbXk="

    async def resume(message):
        return await runtime.resume(WA, identity, message=message)

    await runtime.start("onboarding", WA, identity, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": doc}]})
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": doc}]})
    await resume({"event": "prequalification.completed", "madadScore": 78})
    # Don't upload all docs — leaving documents step incomplete so the
    # reminder is scheduled with the missing list.
    docs_calls = [
        kwargs
        for reason, kwargs in harness.reminders.calls
        if reason == "incomplete_docs"
    ]
    assert len(docs_calls) >= 1
    variables = docs_calls[0].get("variables") or {}
    assert "documents" in variables
    # The WhatsApp checklist enumerates a numbered list — verify there's at
    # least one numbered item rendered.
    assert any(f"{n}." in variables["documents"] for n in range(1, 11))
