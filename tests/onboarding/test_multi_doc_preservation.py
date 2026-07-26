"""Document preservation (user 2026-07-26): a regulated fintech must NEVER lose a
customer document. When several files arrive in ONE message at a single-doc step
(CR or audited financials), the intended file takes its slot and every OTHER file
must be persisted as an ADDITIONAL_DOCUMENT — on WhatsApp or email. This pins the
exact gap the email test hit: 3 attachments at the financials step, only 1 saved.
"""

from __future__ import annotations

from app.shared.workflow import Channel

WA = Channel.WHATSAPP
PHONE = "+97455501199"
CR = "Q1JfZG9j"          # distinct bytes per file so the dup-CR guard never trips
AUDIT = "QVVESVRfZG9j"
EXTRA1 = "RVhUUkExX2RvYw=="
EXTRA2 = "RVhUUkEyX2RvYw=="


def _additional_uploads(kyc) -> set[str]:
    return {
        c[1].get("filename")
        for c in kyc.calls
        if c[0] == "upload_document_base64"
        and c[1].get("document_type") == "ADDITIONAL_DOCUMENT"
    }


async def test_multiple_docs_at_financials_step_are_all_preserved(harness) -> None:
    runtime = harness.platform.runtime
    kyc = harness.kyc

    await runtime.start("onboarding", WA, PHONE, input={"trigger": "campaign"})
    await runtime.resume(WA, PHONE, message={"text": "YES"})
    await runtime.resume(WA, PHONE, message={"text": "biz@example.com"})
    await runtime.resume(
        WA, PHONE, message={"attachments": [{"filename": "CR.pdf", "content_base64": CR}]}
    )
    # Now parked at financials_await. Send the audited report + TWO more files in
    # ONE message — the exact multi-attachment case that dropped extras.
    await runtime.resume(
        WA,
        PHONE,
        message={
            "attachments": [
                {"filename": "Audit.pdf", "content_base64": AUDIT},
                {"filename": "Extra1.pdf", "content_base64": EXTRA1},
                {"filename": "Extra2.pdf", "content_base64": EXTRA2},
            ]
        },
    )

    # The intended audited report went through its own path...
    assert any(
        c[0] == "upload_audited_financial_report" and c[1].get("filename") == "Audit.pdf"
        for c in kyc.calls
    ), "the intended audited report must still be uploaded"

    # ...and BOTH extras were preserved as additional documents — nothing lost.
    additional = _additional_uploads(kyc)
    assert "Extra1.pdf" in additional, f"Extra1 lost — additional={additional}"
    assert "Extra2.pdf" in additional, f"Extra2 lost — additional={additional}"


async def test_single_doc_stashes_nothing(harness) -> None:
    """The common case — one file at the financials step — must NOT create any
    spurious additional-document uploads."""
    runtime = harness.platform.runtime
    kyc = harness.kyc

    await runtime.start("onboarding", WA, PHONE, input={"trigger": "campaign"})
    await runtime.resume(WA, PHONE, message={"text": "YES"})
    await runtime.resume(WA, PHONE, message={"text": "biz@example.com"})
    await runtime.resume(
        WA, PHONE, message={"attachments": [{"filename": "CR.pdf", "content_base64": CR}]}
    )
    await runtime.resume(
        WA, PHONE, message={"attachments": [{"filename": "Audit.pdf", "content_base64": AUDIT}]}
    )
    assert _additional_uploads(kyc) == set(), "single doc must not spawn additional uploads"


async def test_resent_cr_plus_real_audit_promotes_the_audit_not_stall(harness) -> None:
    """Regression for the review's P1: at the financials step a message of
    [re-sent CR, real audited report] must PROMOTE the real report to the
    financials slot (not bury it as a pending extra and stall the run)."""
    runtime = harness.platform.runtime
    kyc = harness.kyc

    await runtime.start("onboarding", WA, PHONE, input={"trigger": "campaign"})
    await runtime.resume(WA, PHONE, message={"text": "YES"})
    await runtime.resume(WA, PHONE, message={"text": "biz@example.com"})
    await runtime.resume(
        WA, PHONE, message={"attachments": [{"filename": "CR.pdf", "content_base64": CR}]}
    )
    # financials step: re-send the CR FIRST, then the real audited report.
    await runtime.resume(
        WA,
        PHONE,
        message={
            "attachments": [
                {"filename": "CR-again.pdf", "content_base64": CR},      # byte-identical CR
                {"filename": "RealAudit.pdf", "content_base64": AUDIT},   # the real report
            ]
        },
    )
    # The real audited report must land in the audited-report slot (not lost/stalled).
    assert any(
        c[0] == "upload_audited_financial_report" and c[1].get("filename") == "RealAudit.pdf"
        for c in kyc.calls
    ), "the real audited report must be promoted to the financials slot"
    # The re-sent CR must NOT be duplicated as an additional document.
    assert "CR-again.pdf" not in _additional_uploads(kyc)


async def test_multiple_docs_at_cr_step_are_all_preserved(harness) -> None:
    runtime = harness.platform.runtime
    kyc = harness.kyc

    await runtime.start("onboarding", WA, PHONE, input={"trigger": "campaign"})
    await runtime.resume(WA, PHONE, message={"text": "YES"})
    await runtime.resume(WA, PHONE, message={"text": "biz@example.com"})
    # CR step: send the CR + an extra doc in one message.
    await runtime.resume(
        WA,
        PHONE,
        message={
            "attachments": [
                {"filename": "CR.pdf", "content_base64": CR},
                {"filename": "ExtraAtCR.pdf", "content_base64": EXTRA1},
            ]
        },
    )
    assert "ExtraAtCR.pdf" in _additional_uploads(kyc), "extra sent with the CR was lost"
