"""Cross-service integration: checklist sourced from the CMS."""

from __future__ import annotations

from app.services.cms import ChecklistItem, build_cms_service
from app.services.document import (
    CmsChecklistProvider,
    InMemoryMadadDocumentGateway,
    build_document_service,
)

APP = "APP-CMS"


async def test_checklist_sourced_from_cms_reflects_updates():
    cms = build_cms_service()
    await cms.upsert_checklist(
        "onboarding", [ChecklistItem(code="trade_license"), ChecklistItem(code="tax_card")]
    )

    service = build_document_service(
        checklist_provider=CmsChecklistProvider(cms),
        gateway=InMemoryMadadDocumentGateway(type_by_keyword={"trade": "trade_license"}),
    )
    await service.ingest_document("Trade_License.pdf", content=b"a", application_ref=APP)

    status = await service.checklist_status("onboarding", application_ref=APP)
    assert status.validated == ["trade_license"]
    assert status.missing == ["tax_card"]

    # Operator adds a new required document in the CMS — reflects immediately.
    await cms.upsert_checklist(
        "onboarding",
        [
            ChecklistItem(code="trade_license"),
            ChecklistItem(code="tax_card"),
            ChecklistItem(code="establishment_card"),
        ],
    )
    status = await service.checklist_status("onboarding", application_ref=APP)
    assert "establishment_card" in status.missing
