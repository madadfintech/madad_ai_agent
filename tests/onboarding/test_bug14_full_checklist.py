"""Bug #14 (UAT 2026-06-09): per-upload message must show FULL cumulative
checklist (✅ done + ⚠️ still needed), not just the latest batch.

The user uploaded every doc except Qatar ID. The bot only acknowledged the
docs in the last batch, never told the SME "Shareholder QID — still needed",
so the user had no way to know what was missing. Spec page 3-4 (Full
Document Submission + Sample Checklist State) explicitly requires the
running checklist + "please send the missing items" prompt after each
upload.
"""

from __future__ import annotations

from app.services.workflow.onboarding import DEFAULT_WHATSAPP_REQUIRED_DOCS
from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455501401"
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


def _last_results(harness, template_key: str) -> str:
    """Pull the most recent rendered results body for the given template."""
    sent = [
        s for s in harness.messenger.sent
        if s["template_key"] == template_key
    ]
    assert sent, f"{template_key} should have fired"
    return sent[-1]["variables"]["results"]


async def test_first_upload_shows_full_checklist_with_remaining_warnings(harness) -> None:
    """One doc uploaded → response shows ✅ for that doc AND ⚠️ for every
    other required doc, with the "please share remaining N" footer."""
    runtime = harness.platform.runtime
    await _drive_to_documents(harness)

    await runtime.resume(
        WA,
        IDENTITY,
        message={
            "attachments": [
                {"filename": "Establishment_Card.pdf", "content_base64": DOC}
            ]
        },
    )

    body = _last_results(harness, "onboarding.documents.single_received")
    # Batch receipt for the uploaded doc.
    assert "✅ Establishment Card — Received & Validated" in body
    # Cumulative checklist header.
    assert "📋 Application checklist:" in body
    # Every other required doc must appear with ⚠️ "still needed".
    assert "⚠️ Shareholder QID — still needed" in body
    assert "⚠️ Shareholder Passport — still needed" in body
    assert "⚠️ Bank Statement (last 6 months) — still needed" in body
    # Footer with explicit remaining count.
    expected_missing = len(DEFAULT_WHATSAPP_REQUIRED_DOCS) - 1
    assert f"remaining {expected_missing}" in body
    assert "documents to move forward" in body


async def test_uploaded_doc_appears_validated_in_subsequent_batch(harness) -> None:
    """After uploading doc A then doc B, the second batch's checklist
    shows BOTH A and B as ✅ (cumulative), not just B."""
    runtime = harness.platform.runtime
    await _drive_to_documents(harness)

    # First upload — Establishment Card.
    await runtime.resume(
        WA,
        IDENTITY,
        message={"attachments": [{"filename": "Establishment.pdf", "content_base64": DOC}]},
    )
    # Second upload — QID.
    await runtime.resume(
        WA,
        IDENTITY,
        message={"attachments": [{"filename": "QID_Shareholder1.pdf", "content_base64": DOC}]},
    )

    body = _last_results(harness, "onboarding.documents.single_received")
    # Batch receipt for the second doc.
    assert "✅ Shareholder QID — Received & Validated" in body
    # Cumulative ✅ for BOTH docs (no ⚠️ on these).
    checklist_section = body.split("📋 Application checklist:")[1]
    assert "✅ Establishment Card" in checklist_section
    assert "✅ Shareholder QID" in checklist_section
    assert "⚠️ Shareholder QID" not in checklist_section


async def test_qid_only_missing_is_called_out_explicitly(harness) -> None:
    """The exact UAT scenario: SME uploads everything except Qatar ID.
    The agent's response must say "⚠️ Shareholder QID — still needed"
    and the footer must say "remaining 1 document".

    Explicit ``document_type`` hints are passed per attachment so the
    test exercises the checklist-rendering path deterministically, not
    the classifier/next-pending heuristic (covered by other tests)."""
    runtime = harness.platform.runtime
    await _drive_to_documents(harness)

    # Every required post-prequal doc EXCEPT qid, with explicit type hints
    # so the in-memory classifier's filename heuristics don't leak into
    # the assertion. Filenames are intentionally generic ("file_001.pdf")
    # so none trigger an unrelated keyword (e.g. "cr" matching
    # "credit_bureau_report"); the workflow falls back to ``document_type``
    # on the attachment.
    docs_to_send = [d for d in DEFAULT_WHATSAPP_REQUIRED_DOCS if d != "qid"]
    for idx, doc_type in enumerate(docs_to_send):
        await runtime.resume(
            WA,
            IDENTITY,
            message={
                "attachments": [
                    {
                        "filename": f"file_{idx:03d}.pdf",
                        "content_base64": DOC,
                        "document_type": doc_type,
                    }
                ]
            },
        )

    body = _last_results(harness, "onboarding.documents.single_received")
    assert "📋 Application checklist:" in body
    # QID must be explicitly flagged.
    assert "⚠️ Shareholder QID — still needed" in body
    # Footer must reflect ONE remaining.
    assert "remaining 1 document" in body
    # And the coffee message must NOT have fired yet — checklist still open.
    templates = harness.messenger.templates()
    assert "onboarding.documents.complete" not in templates


async def test_processing_ack_template_is_generic_and_short(harness) -> None:
    """The pre-ack must NOT assume a ZIP or be wordy. UAT feedback
    explicitly called this out."""
    runtime = harness.platform.runtime
    await _drive_to_documents(harness)
    await runtime.resume(
        WA,
        IDENTITY,
        message={"attachments": [{"filename": "Establishment.pdf", "content_base64": DOC}]},
    )

    # RecordingMessenger doesn't render templates, so we can't inspect
    # the body — but the seed-templates file is the source of truth and
    # the live deploy re-seeds. Pin the template body via the seed
    # module itself so a future regression on either side is caught.
    from scripts.seed_cms_templates import _TEMPLATE_BODIES
    body = _TEMPLATE_BODIES["onboarding.documents.processing"]
    assert "ZIP" not in body, "processing ack must not assume the upload is a ZIP"
    assert len(body) < 100, "processing ack should be short — got " + body
