"""A6 — structured offer cards rendered from OFFERS_GET_MY_OFFERS schema.

Per Ishan (2026-06-07): each offer carries lender, creditLimit, interestRate,
tenureDays, processingFee, expiryDate. The agent's offers.preview message
renders one card per offer at PDF Step 8 fidelity.
"""

from __future__ import annotations

from app.services.workflow.onboarding import _format_offer_cards


def test_format_offer_cards_renders_pdf_step_8_layout() -> None:
    """Two offers matching the PDF example render as two banded cards
    separated by the box-drawing rule."""
    offers = [
        {
            "lender": "Qatar Islamic Bank",
            "creditLimit": 35000,
            "interestRate": 10,
            "tenureDays": 30,
            "processingFee": 100,
        },
        {
            "lender": "Commercial Bank",
            "creditLimit": 40000,
            "interestRate": 9.5,
            "tenureDays": 45,
            "processingFee": 150,
        },
    ]
    out = _format_offer_cards(offers)
    # Card 1: PDF Step 8 first row
    assert "🏦 Offer 1 — Qatar Islamic Bank" in out
    assert "QAR 35,000" in out
    assert "10% p.a." in out
    assert "30 days" in out
    assert "QAR 100 fee" in out
    # Card 2: PDF Step 8 second row
    assert "🏦 Offer 2 — Commercial Bank" in out
    assert "QAR 40,000" in out
    assert "9.5% p.a." in out
    assert "45 days" in out
    assert "QAR 150 fee" in out
    # Separator rule between cards
    assert "━━━━━━━━━━━━━" in out


def test_format_offer_cards_tolerates_snake_case_keys() -> None:
    """Test fixtures + older capstone shapes use snake_case — render them
    too without crashing."""
    offers = [
        {
            "lender": "Lender A",
            "credit_limit": 50000,
            "interest_rate": 8.5,
            "tenure_days": 60,
            "processing_fee": 200,
        },
    ]
    out = _format_offer_cards(offers)
    assert "🏦 Offer 1 — Lender A" in out
    assert "QAR 50,000" in out
    assert "8.5% p.a." in out
    assert "60 days" in out


def test_format_offer_cards_missing_fields_degrade_gracefully() -> None:
    """Real backend may not populate every field on every offer — render the
    em-dash placeholder rather than crashing or showing 'None'."""
    offers = [{"lender": "Lender A"}]  # everything else missing
    out = _format_offer_cards(offers)
    assert "Lender A" in out
    assert "None" not in out
    assert "—" in out


def test_format_offer_cards_empty_returns_empty_string() -> None:
    """When the workflow hasn't fetched offers yet, the template's
    {{ offer_cards }} substitutes to '' (no rendering, no error)."""
    assert _format_offer_cards([]) == ""


def test_format_offer_cards_missing_fee_says_no_fee() -> None:
    """If processingFee is absent, say 'no fee' explicitly."""
    offers = [
        {
            "lender": "Bank Free",
            "creditLimit": 25000,
            "interestRate": 6,
            "tenureDays": 90,
            # no processingFee
        }
    ]
    out = _format_offer_cards(offers)
    assert "no fee" in out
    assert "Bank Free" in out
