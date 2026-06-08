"""A8a + A8b — bank names in payment-confirmed (Step 6) + activated (Step 9).

Per Ishan (2026-06-07):
  * BusinessDetails.banksToSend (admin sets at QUALIFIED forward) → Step 6 list.
  * Accepted offer.lender / .creditLimit / .interestRate / .tenureDays → Step 9 card.
"""

from __future__ import annotations

from app.services.workflow.onboarding import _format_banks_list

# -- A8a banks list formatter --------------------------------------------------


def test_format_banks_list_two_banks() -> None:
    assert _format_banks_list(["Qatar Islamic Bank", "Commercial Bank"]) == (
        "Qatar Islamic Bank and Commercial Bank"
    )


def test_format_banks_list_three_banks_uses_oxford_comma() -> None:
    assert _format_banks_list(["QIB", "QNB", "Commercial Bank"]) == (
        "QIB, QNB, and Commercial Bank"
    )


def test_format_banks_list_single_bank() -> None:
    assert _format_banks_list(["Commercial Bank"]) == "Commercial Bank"


def test_format_banks_list_empty_falls_back_to_generic() -> None:
    """The PDF Step 6 message reads cleanly even when banksToSend hasn't
    populated yet — the helper gives a graceful fallback string."""
    assert _format_banks_list([]) == "our banking partners"


def test_format_banks_list_strips_empties() -> None:
    """A backend that returns [\"QIB\", None, \"\"] still renders sanely."""
    assert _format_banks_list(["QIB", "", "Commercial Bank"]) == (
        "QIB and Commercial Bank"
    )
