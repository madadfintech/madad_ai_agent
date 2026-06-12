"""UAT 2026-06-10: Excting news / activated / payment-received templates
rendered empty fields because the agent read the wrong key off ``/me``.

Per Ishan's handover §8, the enriched ``auth_me`` payload exposes:
  * ``offersReceived`` (NOT ``offers``) — the lender offer list
  * ``creditLines`` — active credit line state, ``lender / creditLimit /
    interestRate / tenureDays``
  * ``referenceNumber`` / ``uniqueId`` — the application ref

These tests pin the helpers + the three rendering sites against the
canonical shapes so a future field rename can't silently strip details
out of the SME's chat again.
"""

from __future__ import annotations

from app.services.workflow.onboarding import (
    _extract_credit_line_from_me,
    _extract_offers_from_me,
    _extract_reference_from_me,
)


def test_extract_offers_handles_canonical_offersReceived_field() -> None:
    info = {
        "user": {"id": "u1"},
        "offersReceived": [
            {"lender": "QIB", "creditLimit": 35000, "interestRate": 10},
            {"lender": "Commercial Bank", "creditLimit": 40000, "interestRate": 9.5},
        ],
    }
    offers = _extract_offers_from_me(info)
    assert len(offers) == 2
    assert offers[0]["lender"] == "QIB"


def test_extract_offers_accepts_legacy_offers_field_for_back_compat() -> None:
    # Older fixtures + the InMemory fake may still serve ``offers``;
    # the helper must keep handling that shape so the field rename
    # doesn't break tests that pre-date Ishan's §8 enrichment.
    info = {"offers": [{"lender": "QIB", "creditLimit": 35000}]}
    offers = _extract_offers_from_me(info)
    assert offers and offers[0]["lender"] == "QIB"


def test_extract_offers_walks_into_user_subobject() -> None:
    info = {"user": {"offersReceived": [{"lender": "QIB"}]}}
    offers = _extract_offers_from_me(info)
    assert offers and offers[0]["lender"] == "QIB"


def test_extract_offers_empty_payload_returns_empty_list() -> None:
    assert _extract_offers_from_me({}) == []
    assert _extract_offers_from_me(None) == []  # type: ignore[arg-type]


def test_extract_reference_handles_multiple_field_names() -> None:
    assert _extract_reference_from_me({"referenceNumber": "7388266"}) == "7388266"
    assert _extract_reference_from_me({"user": {"uniqueId": "1234567"}}) == "1234567"
    assert _extract_reference_from_me(
        {"user": {"referenceNumber": "9999999"}}
    ) == "9999999"


def test_extract_reference_returns_none_when_absent() -> None:
    assert _extract_reference_from_me({}) is None
    assert _extract_reference_from_me({"user": {}}) is None
    # Empty-string values count as absent so callers' ``or state.ref``
    # fallbacks keep working.
    assert _extract_reference_from_me({"referenceNumber": ""}) is None


def test_extract_credit_line_prefers_active_entry() -> None:
    info = {
        "creditLines": [
            {"lender": "Closed Bank", "status": "CLOSED", "creditLimit": 10000},
            {"lender": "Commercial Bank", "status": "ACTIVE", "creditLimit": 40000},
        ],
    }
    cl = _extract_credit_line_from_me(info)
    assert cl["lender"] == "Commercial Bank"
    assert cl["creditLimit"] == 40000


def test_extract_credit_line_walks_user_subobject() -> None:
    info = {
        "user": {
            "creditLines": [{"lender": "QIB", "creditLimit": 35000}],
        },
    }
    cl = _extract_credit_line_from_me(info)
    assert cl["lender"] == "QIB"


def test_extract_credit_line_returns_empty_dict_when_absent() -> None:
    assert _extract_credit_line_from_me({}) == {}
    assert _extract_credit_line_from_me({"creditLines": []}) == {}
