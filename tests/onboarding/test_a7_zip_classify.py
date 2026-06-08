"""A7 — ZIP uploads route through classify_and_upload_zip_base64.

Per Ishan (2026-06-07): the backend unzips server-side, classifies every
member, and returns the per-file checklist. One call instead of N
(one per member) — matches the msme-portal pipeline exactly.
"""

from __future__ import annotations

import base64
import io
import zipfile

from app.services.workflow import InMemoryKycClient
from app.shared.workflow import Channel

WA = Channel.WHATSAPP


def _build_zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("Trade_License.pdf", b"dummy CR bytes")
        zf.writestr("Tax_Card.pdf", b"dummy tax bytes")
        zf.writestr("Bank_Statement.pdf", b"dummy bank bytes")
    return buffer.getvalue()


async def test_zip_upload_routes_through_classify_and_upload_zip(make_harness) -> None:
    """When a ZIP attachment arrives at the documents step, the workflow
    calls classify_and_upload_zip_base64 ONCE (not per-member)."""

    class _ZipKyc(InMemoryKycClient):
        async def classify_and_upload_zip_base64(  # type: ignore[override]
            self,
            *,
            access_token: str,
            content_base64: str,
            filename: str,
            continue_on_error: bool = True,
        ) -> dict[str, object]:
            self._record(
                "classify_and_upload_zip_base64",
                access_token=access_token,
                filename=filename,
            )
            return {
                "checklist": [
                    {"file_name": "Trade_License.pdf", "document_type": "TRADE_LICENSE"},
                    {"file_name": "Tax_Card.pdf", "document_type": "TAX_CARD"},
                    {"file_name": "Bank_Statement.pdf", "document_type": "BANK_STATEMENT"},
                ]
            }

    harness = make_harness()
    harness.platform.workflow._kyc = _ZipKyc(  # type: ignore[union-attr]
        required_documents=["trade_license", "tax_card", "bank_statement"]
    )
    harness.kyc = harness.platform.workflow._kyc  # type: ignore[union-attr]
    runtime = harness.platform.runtime
    identity = "+97455500A7Z"
    doc = "ZHVtbXk="

    async def resume(message):
        return await runtime.resume(WA, identity, message=message)

    await runtime.start("onboarding", WA, identity, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": doc}]})
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": doc}]})
    await resume({"event": "prequalification.completed", "madadScore": 78})

    zip_bytes = _build_zip_bytes()
    zip_b64 = base64.b64encode(zip_bytes).decode("ascii")
    await resume(
        {
            "attachments": [
                {
                    "filename": "documents.zip",
                    "content_base64": zip_b64,
                    "mime_type": "application/zip",
                }
            ]
        }
    )

    kyc_calls = [name for name, _ in harness.kyc.calls]
    # The ZIP tool fires exactly once for the whole archive.
    assert kyc_calls.count("classify_and_upload_zip_base64") == 1
    # And the per-file path did NOT fire 3 extra times (server-side handled it).
    assert kyc_calls.count("classify_and_upload_document_base64") == 0


async def test_zip_fallback_when_backend_errors(make_harness) -> None:
    """If classify_and_upload_zip_base64 raises (backend temporarily down),
    the workflow falls back to local unzipping + per-file classify so the
    SME's upload isn't lost."""

    class _FailingZipKyc(InMemoryKycClient):
        async def classify_and_upload_zip_base64(  # type: ignore[override]
            self,
            *,
            access_token: str,
            content_base64: str,
            filename: str,
            continue_on_error: bool = True,
        ) -> dict[str, object]:
            self._record(
                "classify_and_upload_zip_base64",
                access_token=access_token,
                filename=filename,
            )
            raise RuntimeError("backend zip tool unavailable")

    harness = make_harness()
    harness.platform.workflow._kyc = _FailingZipKyc(  # type: ignore[union-attr]
        required_documents=["trade_license", "tax_card", "bank_statement"]
    )
    harness.kyc = harness.platform.workflow._kyc  # type: ignore[union-attr]
    runtime = harness.platform.runtime
    identity = "+97455500A7F"
    doc = "ZHVtbXk="

    async def resume(message):
        return await runtime.resume(WA, identity, message=message)

    await runtime.start("onboarding", WA, identity, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": doc}]})
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": doc}]})
    await resume({"event": "prequalification.completed", "madadScore": 78})

    zip_bytes = _build_zip_bytes()
    zip_b64 = base64.b64encode(zip_bytes).decode("ascii")
    await resume(
        {
            "attachments": [
                {
                    "filename": "documents.zip",
                    "content_base64": zip_b64,
                    "mime_type": "application/zip",
                }
            ]
        }
    )

    kyc_calls = [name for name, _ in harness.kyc.calls]
    # The ZIP tool was attempted once + raised...
    assert kyc_calls.count("classify_and_upload_zip_base64") == 1
    # ...and the workflow fell back to per-member classify (3 members).
    assert kyc_calls.count("classify_and_upload_document_base64") == 3
