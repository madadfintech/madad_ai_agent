"""Bug #1b (2026-06-09): post-prequal document upload silence.

QA on +91 9497191690 uploaded a 7 MB ZIP after the documents.checklist;
``madad_kyc_classify_and_upload_zip_base64`` hung for ~3 minutes inside the
node, the workflow runtime timed it out (`RetryExhaustedError`), and the
SME got no acknowledgement at all. Same shape as Bug #1.

Fix mirrors the CR ack pattern:
  1. ``onboarding.documents.processing`` fires the instant valid
     attachment(s) are detected, before any MCP work.
  2. The ZIP + per-file classify calls now run under a hard
     ``asyncio.wait_for(25s)`` cap so a single hung call can't trap the
     node; on overrun the ZIP path falls back to local-unzip.
  3. ``_acknowledge_uploads`` sends ``onboarding.documents.upload_failed``
     when nothing made it past the classifier instead of silently exiting.
"""

from __future__ import annotations

from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455500001"
DOC = "ZHVtbXk="


async def _drive_to_documents(harness, runtime) -> None:
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})
    await runtime.resume(WA, IDENTITY, message={"text": "biz@example.com"})  # business_email
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


async def test_valid_doc_triggers_immediate_processing_ack(harness) -> None:
    runtime = harness.platform.runtime
    await _drive_to_documents(harness, runtime)

    # Clear any prior templates so we only see the docs-loop sends.
    before = list(harness.messenger.templates())

    await runtime.resume(
        WA,
        IDENTITY,
        message={
            "attachments": [
                {
                    "filename": "national_address.pdf",
                    "content_base64": DOC,
                    "mime_type": "application/pdf",
                }
            ]
        },
    )

    new_templates = harness.messenger.templates()[len(before):]
    # The processing ack must fire before any per-doc checklist render.
    assert "onboarding.documents.processing" in new_templates
    proc_ix = new_templates.index("onboarding.documents.processing")
    # The per-doc ack (zip_received or single_received) follows after.
    follow_up = next(
        (
            t
            for t in new_templates[proc_ix + 1 :]
            if t
            in {
                "onboarding.documents.zip_received",
                "onboarding.documents.single_received",
                "onboarding.documents.upload_failed",
            }
        ),
        None,
    )
    assert follow_up is not None
