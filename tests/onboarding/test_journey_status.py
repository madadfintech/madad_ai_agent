"""The canonical 16-status enum mirrors Ishan's MCP cluster README."""

from __future__ import annotations

from app.services.workflow.state import (
    TERMINAL_FAIL_STATUSES,
    TERMINAL_SUCCESS_STATUSES,
    JourneyStatus,
)


def test_journey_status_enum_has_all_sixteen_canonical_values() -> None:
    expected = {
        "SIGN_UP",
        "ONBOARDED",
        "IN_ELIGIBLE",
        "ELIGIBLE",
        "INCOMPLETE",
        "UNVERIFIED",
        "VERIFIED",
        "PRE_QUALIFIED",
        "QUALIFIED",
        "UNQUALIFIED",
        "ACCEPTED",
        "NOT_ACCEPTED",
        "OFFER_ACCEPTED",
        "OFFER_EXPIRED",
        "OPEN",
        "ACTIVATED",
    }
    assert {s.value for s in JourneyStatus} == expected
    assert {s.name for s in JourneyStatus} == expected
    assert len(JourneyStatus) == 16


def test_journey_status_is_string_subtype() -> None:
    # StrEnum members compare equal to their string value — important because
    # ``madad_auth_me`` returns the raw string.
    assert JourneyStatus.ACTIVATED == "ACTIVATED"
    assert JourneyStatus("PRE_QUALIFIED") is JourneyStatus.PRE_QUALIFIED


def test_terminal_status_groups_are_distinct() -> None:
    assert TERMINAL_SUCCESS_STATUSES == {JourneyStatus.ACTIVATED}
    assert TERMINAL_FAIL_STATUSES == {
        JourneyStatus.IN_ELIGIBLE,
        JourneyStatus.UNQUALIFIED,
        JourneyStatus.NOT_ACCEPTED,
    }
    assert TERMINAL_SUCCESS_STATUSES.isdisjoint(TERMINAL_FAIL_STATUSES)
