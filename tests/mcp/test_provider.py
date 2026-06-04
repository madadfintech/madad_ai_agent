"""``get_mcp_client()`` populates a sensible default ``idempotent_tools`` set."""

from __future__ import annotations

import pytest

from app.shared.mcp import Tools
from app.shared.mcp.client import HttpMCPClient
from app.shared.mcp.provider import _default_idempotent_tools, get_mcp_client


@pytest.fixture(autouse=True)
def _clear_cache():
    get_mcp_client.cache_clear()
    yield
    get_mcp_client.cache_clear()


def test_default_set_covers_reads_and_payment_idempotent_writes() -> None:
    default = _default_idempotent_tools()
    # Reads — chosen exemplars from each module that returns data.
    assert Tools.AUTH_ME in default
    assert Tools.KYC_GET_BUSINESS_DETAILS in default
    assert Tools.OFFERS_GET_MY_OFFERS in default
    assert Tools.PAYMENTS_GET_MONETIZATION_PAYMENT in default
    # Payment writes that backend now honours idempotency_key on.
    assert Tools.PAYMENTS_CREATE_MONETIZATION_PAYMENT in default
    assert Tools.PAYMENTS_SEND_MONETIZATION_PAYMENT_LINK in default
    # Writes that are NOT yet backend-idempotent must stay out.
    assert Tools.KYC_UPLOAD_DOCUMENT_BASE64 not in default
    assert Tools.KYC_UPDATE_ELIGIBILITY not in default
    assert Tools.KYC_ADD_SHAREHOLDERS not in default


def test_get_mcp_client_populates_default_when_settings_empty(monkeypatch) -> None:
    from app.core.config import McpSettings, settings

    monkeypatch.setattr(
        settings, "mcp", McpSettings(endpoint="https://mcp.example", idempotent_tools=set())
    )
    client = get_mcp_client()
    assert isinstance(client, HttpMCPClient)
    s = client._settings  # private but acceptable for a wiring test
    assert s.idempotent_tools == _default_idempotent_tools()


def test_operator_supplied_set_is_preserved(monkeypatch) -> None:
    from app.core.config import McpSettings, settings

    custom = {Tools.AUTH_ME}  # operator-chosen narrower set
    monkeypatch.setattr(
        settings,
        "mcp",
        McpSettings(endpoint="https://mcp.example", idempotent_tools=custom),
    )
    client = get_mcp_client()
    assert client._settings.idempotent_tools == custom
