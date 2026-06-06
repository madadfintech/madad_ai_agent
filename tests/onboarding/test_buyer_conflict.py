"""Buyer step: backend 409 (buyer already exists) is treated as soft-success.

Per Ishan's handover note (2026-06-06), the buyer step against a reused test
SME returns HTTP 409 from ``madad_kyc_add_buyer`` because the same buyer is
already on the SME's record. The workflow must advance cleanly and not block
the rest of the journey.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.shared.mcp import MCPError
from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
IDENTITY = "+97455500777"


async def _drive_until_buyer(harness, runtime) -> None:
    async def resume(message: dict[str, Any]):
        return await runtime.resume(WA, IDENTITY, message=message)

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"first_name": "Aisha", "last_name": "Karim"})
    await resume({"attachments": [{"filename": "CR.pdf"}]})
    await resume({"annual_revenue_qar": 1_000_000, "sector": "trade"})
    await resume({"attachments": [{"filename": "Audited.pdf"}]})


async def test_add_buyer_409_advances_and_does_not_warn(harness) -> None:
    runtime = harness.platform.runtime
    await _drive_until_buyer(harness, runtime)

    # Replace add_buyer with a stub that simulates the backend 409 returned
    # for a reused test SME. The MCPError mirrors the production shape: a
    # generic wrapper message with the upstream HTTP status in the cause.
    async def add_buyer_raises_409(*, access_token: str, data: dict[str, Any]) -> dict[str, Any]:
        raise MCPError(
            "MCP tool 'madad_kyc_add_buyer' failed after 1 attempt(s)",
            details={"tool": "madad_kyc_add_buyer"},
        ) from RuntimeError(
            "Error calling tool 'madad_kyc_add_buyer': Madad API returned HTTP 409"
        )

    harness.kyc.add_buyer = add_buyer_raises_409  # type: ignore[method-assign]

    with structlog.testing.capture_logs() as captured:
        result = await runtime.resume(
            WA, IDENTITY, message={"name": "ACME Trading LLC", "buyer_type": "DOMESTIC"}
        )

    # Workflow advanced to the shareholders step despite the 409.
    assert result.prompt == {"waiting_for": "shareholders", "step": "shareholders"}
    assert result.status == RunStatus.WAITING_FOR_INPUT

    # A 409 was logged as INFO ("already_exists"), not WARNING ("failed").
    events = [c.get("event") for c in captured]
    assert "add_buyer.already_exists" in events
    assert "add_buyer.failed" not in events


async def test_add_buyer_non_409_error_still_advances_with_warning(harness) -> None:
    """Non-409 failures keep the staging-tolerant 'advance + warn' behavior."""
    runtime = harness.platform.runtime
    await _drive_until_buyer(harness, runtime)

    async def add_buyer_raises_500(*, access_token: str, data: dict[str, Any]) -> dict[str, Any]:
        raise MCPError(
            "MCP tool 'madad_kyc_add_buyer' failed after 1 attempt(s)",
            details={"tool": "madad_kyc_add_buyer"},
        ) from RuntimeError("Madad API returned HTTP 500")

    harness.kyc.add_buyer = add_buyer_raises_500  # type: ignore[method-assign]

    with structlog.testing.capture_logs() as captured:
        result = await runtime.resume(
            WA, IDENTITY, message={"name": "ACME", "buyer_type": "DOMESTIC"}
        )

    assert result.prompt == {"waiting_for": "shareholders", "step": "shareholders"}
    events = [c.get("event") for c in captured]
    assert "add_buyer.failed" in events
    assert "add_buyer.already_exists" not in events
