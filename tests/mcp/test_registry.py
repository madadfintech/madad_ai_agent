"""The tool registry is the single source of truth for the catalog."""

from __future__ import annotations

from app.shared.mcp import Tools


def test_registry_has_70_constants_grouped_by_module() -> None:
    names = Tools.all()
    # 13 + 6 + 5 + 28 + 4 + 5 + 9 = 70. +2 vs the prior count came from
    # MCP_UPDATE_ONBOARDING_PROGRESS + MCP_GET_ONBOARDING_PROGRESS (added
    # 2026-06-07 by Ishan to support WhatsApp lead step tracking).
    assert len(names) == 70

    by_prefix: dict[str, int] = {}
    for value in names.values():
        prefix = value.rsplit("madad_", 1)[-1].split("_", 1)[0]
        by_prefix[prefix] = by_prefix.get(prefix, 0) + 1

    assert by_prefix["auth"] == 13
    assert by_prefix["external"] == 6
    assert by_prefix["mcp"] == 5
    assert by_prefix["kyc"] == 28
    assert by_prefix["offers"] == 4
    assert by_prefix["payments"] == 5
    assert by_prefix["invoices"] == 9


def test_complete_stage_is_intentionally_omitted() -> None:
    # Per Ishan Q9 — complete_stage is a backup tool and must not be used.
    for value in Tools.all().values():
        assert value != "madad_kyc_complete_stage"


def test_all_names_are_namespaced_and_unique() -> None:
    values = list(Tools.all().values())
    assert all(v.startswith("madad_") for v in values)
    assert len(values) == len(set(values))  # no duplicates


def test_key_entry_constants_present() -> None:
    # Spot-check the constants the implementation plan calls out by name.
    assert Tools.MCP_CREATE_CHANNEL_SESSION == "madad_mcp_create_channel_session"
    assert Tools.KYC_UPLOAD_DOCUMENT_BASE64 == "madad_kyc_upload_document_base64"
    assert Tools.AUTH_ME == "madad_auth_me"
    assert Tools.EXT_SEND_WHATSAPP_TEXT == "madad_external_send_whatsapp_text"
    assert Tools.PAYMENTS_CREATE_MONETIZATION_PAYMENT == (
        "madad_payments_create_monetization_payment"
    )


def test_read_only_helper_covers_status_and_get_tools() -> None:
    safe = Tools.read_only()
    assert Tools.AUTH_ME in safe
    assert Tools.KYC_GET_BUSINESS_DETAILS in safe
    assert Tools.OFFERS_GET_MY_OFFERS in safe
    # Writes are NOT in the safe set.
    assert Tools.KYC_UPLOAD_DOCUMENT_BASE64 not in safe
    assert Tools.PAYMENTS_CREATE_MONETIZATION_PAYMENT not in safe


def test_payment_idempotent_writes_helper_lists_the_two_write_tools() -> None:
    assert Tools.payment_idempotent_writes() == {
        Tools.PAYMENTS_CREATE_MONETIZATION_PAYMENT,
        Tools.PAYMENTS_SEND_MONETIZATION_PAYMENT_LINK,
    }
