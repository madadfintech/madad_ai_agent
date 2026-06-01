"""Workflow MCP adapters: per-method wiring + a full onboarding run via MCP.

The capstone proves the CATALOG-DEPENDENT scaffolds drive the real onboarding
graph end-to-end against a fake MCP caller — so go-live is "confirm tool
names/schemas + flip mcp.enabled", not new adapter work.
"""

from __future__ import annotations

from app.services.workflow import (
    McpDocumentIntake,
    McpMadadClient,
    McpPaymentClient,
    RecordingMessenger,
    RecordingReminders,
    build_onboarding_platform,
)
from app.shared.mcp import InMemoryMCPClient, Tools
from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
IDENTITY = "+97455500077"


# -- per-adapter wiring ------------------------------------------------------


async def test_madad_client_maps_methods_to_tools():
    caller = InMemoryMCPClient(
        handlers={
            Tools.ELIGIBILITY_CHECK: lambda p: {"eligible": True},
            Tools.PREQUAL_REQUEST: lambda p: {"application_ref": "app_1"},
            Tools.CREDITLINE_ACTIVATE: lambda p: {"active": True, "limit": 50000},
        }
    )
    madad = McpMadadClient(caller)

    assert await madad.check_eligibility("cr_1") is True
    assert await madad.request_prequalification(identity=IDENTITY, cr_ref="cr_1") == "app_1"
    await madad.request_score("app_1")
    await madad.submit_to_lenders("app_1")
    activated = await madad.activate_credit_line("app_1", {"offer_id": "o1"})
    assert activated["active"] is True

    tools_called = [name for name, _ in caller.calls]
    assert tools_called == [
        Tools.ELIGIBILITY_CHECK,
        Tools.PREQUAL_REQUEST,
        Tools.SCORE_REQUEST,
        Tools.LENDERS_SUBMIT,
        Tools.CREDITLINE_ACTIVATE,
    ]


async def test_payment_client_creates_link():
    caller = InMemoryMCPClient(
        handlers={Tools.PAYMENT_CREATE_LINK: lambda p: {"link": f"https://pay/{p['amount']}"}}
    )
    link = await McpPaymentClient(caller).create_link(application_ref="app_1", amount=6000)
    assert link == "https://pay/6000"
    assert caller.calls[0][1] == {"application_ref": "app_1", "amount": 6000}


async def test_document_intake_routes_and_reads_checklist():
    caller = InMemoryMCPClient(
        handlers={Tools.DOCUMENT_CHECKLIST: lambda p: {"missing": ["trade_license"]}}
    )
    intake = McpDocumentIntake(caller)
    await intake.ingest(application_ref="app_1", filename="trade.pdf", provider_ref="ref1")
    missing = await intake.missing(checklist="onboarding", application_ref="app_1")

    assert caller.calls[0][0] == Tools.DOCUMENT_PROCESS
    assert caller.calls[0][1]["provider_ref"] == "ref1"
    assert missing == ["trade_license"]


# -- capstone: full onboarding driven through MCP adapters --------------------


def _capstone_caller() -> InMemoryMCPClient:
    return InMemoryMCPClient(
        handlers={
            Tools.ELIGIBILITY_CHECK: lambda p: {"eligible": True},
            Tools.PREQUAL_REQUEST: lambda p: {"application_ref": "app_mcp_1"},
            Tools.SCORE_REQUEST: lambda p: {},
            Tools.LENDERS_SUBMIT: lambda p: {},
            Tools.CREDITLINE_ACTIVATE: lambda p: {"active": True},
            Tools.PAYMENT_CREATE_LINK: lambda p: {"link": "https://pay.tess/x"},
            Tools.DOCUMENT_PROCESS: lambda p: {},
            Tools.DOCUMENT_CHECKLIST: lambda p: {"missing": []},
        }
    )


async def test_full_onboarding_runs_through_mcp_adapters():
    caller = _capstone_caller()
    platform = build_onboarding_platform(
        messenger=RecordingMessenger(),
        reminders=RecordingReminders(),
        documents=McpDocumentIntake(caller),
        madad=McpMadadClient(caller),
        payments=McpPaymentClient(caller),
    )
    runtime = platform.runtime

    start = await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    assert start.waiting

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    await resume({"text": "YES"})
    await resume({"attachments": [{"filename": "CR.pdf"}]})
    await resume({"attachments": [{"filename": "Audited.pdf"}]})
    await resume({"type": "prequalification", "qualified": True})
    await resume({"attachments": [{"filename": "Trade.pdf"}]})
    await resume({"type": "score", "score": 80, "qualified": True})
    await resume({"type": "payment", "paid": True})
    await resume({"type": "offers", "offers": [{"offer_id": "o1"}]})
    result = await resume({"type": "offer_selection", "offer_id": "o1"})

    assert result.status == RunStatus.COMPLETED
    assert result.values["credit_line_active"] is True

    # Every financing decision + payment + document op was routed through MCP.
    called = [name for name, _ in caller.calls]
    for tool in (
        Tools.ELIGIBILITY_CHECK,
        Tools.PREQUAL_REQUEST,
        Tools.DOCUMENT_PROCESS,
        Tools.DOCUMENT_CHECKLIST,
        Tools.SCORE_REQUEST,
        Tools.PAYMENT_CREATE_LINK,
        Tools.LENDERS_SUBMIT,
        Tools.CREDITLINE_ACTIVATE,
    ):
        assert tool in called

    # The financing pipeline ran in the right order.
    order = [
        Tools.ELIGIBILITY_CHECK,
        Tools.PREQUAL_REQUEST,
        Tools.SCORE_REQUEST,
        Tools.LENDERS_SUBMIT,
        Tools.CREDITLINE_ACTIVATE,
    ]
    positions = [called.index(t) for t in order]
    assert positions == sorted(positions)
