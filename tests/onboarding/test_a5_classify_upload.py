"""A5 — KYC document uploads route through classify_and_upload_document_base64.

Per Ishan (2026-06-07): preferred for WhatsApp/email where the SME doesn't
say which doc type they're sending. Backend classifier picks the type, routes
to the right entity slot, returns the resolved ``document_type``.
"""

from __future__ import annotations

from app.shared.workflow import Channel

WA = Channel.WHATSAPP


async def test_docs_loop_uses_classify_and_upload(harness) -> None:
    """A WhatsApp doc upload during the documents step routes through
    classify_and_upload_document_base64, NOT upload_document_base64."""
    runtime = harness.platform.runtime
    identity = "+97455500A5A"
    doc = "ZHVtbXk="

    async def resume(message):
        return await runtime.resume(WA, identity, message=message)

    await runtime.start("onboarding", WA, identity, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"text": "biz@example.com"})  # business_email
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

    kyc_calls = [name for name, _ in harness.kyc.calls]
    # The classify path fires for each KYC doc upload (2 calls = 2 attachments).
    assert kyc_calls.count("classify_and_upload_document_base64") >= 2


async def test_classify_response_resolves_to_workflow_doc_type(harness) -> None:
    """The InMemory classifier returns a snake_case workflow doc_type from
    filename keywords — the workflow uses it directly for missing-docs
    accounting without re-applying our own filename inference."""
    runtime = harness.platform.runtime
    identity = "+97455500A5B"
    doc = "ZHVtbXk="

    async def resume(message):
        return await runtime.resume(WA, identity, message=message)

    await runtime.start("onboarding", WA, identity, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"text": "biz@example.com"})  # business_email
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": doc}]})
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": doc}]})
    await resume({"event": "prequalification.completed", "madadScore": 78})
    # Upload a generically-named file — keyword 'trade' triggers the
    # in-memory classifier to return 'trade_license'.
    await resume(
        {
            "attachments": [
                {"filename": "trade_doc.jpg", "content_base64": doc},
                {"filename": "tax_thing.pdf", "content_base64": doc},
            ]
        }
    )

    classify_calls = [
        kwargs
        for name, kwargs in harness.kyc.calls
        if name == "classify_and_upload_document_base64"
    ]
    # Both classified uploads landed (one per attachment).
    assert len(classify_calls) >= 2
    # The InMemory classifier stored each under the resolved type.
    assert "trade_license" in harness.kyc.uploaded_documents
    assert "tax_card" in harness.kyc.uploaded_documents
