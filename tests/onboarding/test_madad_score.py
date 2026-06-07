"""Madad Score capture + band rendering — A4 from Sprint A.

Per Ishan (2026-06-07), only ``prequalification.completed`` is auto-emitted
today and its payload is ``{ madadScore }`` (number or null). The dispatcher
merges the raw payload into the resume payload via :func:`translate_backend_event`,
so the score arrives camelCase. The workflow's status-await nodes call
:func:`_extract_madad_score` to capture it into ``OnboardingState.madad_score``;
the Step 5 payment-gate node renders it as the PDF's Madad Score card.
"""

from __future__ import annotations

from app.services.workflow.onboarding import _extract_madad_score, _score_band


def test_extract_madad_score_from_camelcase_key() -> None:
    """Real Madad payload uses ``madadScore`` (camelCase)."""
    assert _extract_madad_score({"madadScore": 78}) == 78


def test_extract_madad_score_from_snake_case_key() -> None:
    """Tolerate snake-case copies (test fixtures, manual triggers)."""
    assert _extract_madad_score({"madad_score": 78}) == 78


def test_extract_madad_score_null_returns_none() -> None:
    """Backend may emit ``{"madadScore": null}`` when score is not yet computed."""
    assert _extract_madad_score({"madadScore": None}) is None


def test_extract_madad_score_missing_returns_none() -> None:
    assert _extract_madad_score({"otherField": 1}) is None


def test_extract_madad_score_non_dict_returns_none() -> None:
    assert _extract_madad_score("not-a-dict") is None
    assert _extract_madad_score(None) is None


def test_extract_madad_score_string_coerced_to_int() -> None:
    """Some backends emit numeric strings; coerce them."""
    assert _extract_madad_score({"madadScore": "85"}) == 85


def test_extract_madad_score_bool_rejected() -> None:
    """Reject booleans even though they're int subclass in Python — a backend
    bug returning True/False would otherwise round to 1/0 silently."""
    assert _extract_madad_score({"madadScore": True}) is None
    assert _extract_madad_score({"madadScore": False}) is None


def test_extract_madad_score_invalid_string_returns_none() -> None:
    assert _extract_madad_score({"madadScore": "not-a-number"}) is None


def test_score_band_strong_at_78() -> None:
    """PDF example: 78 → Strong."""
    assert _score_band(78) == "Strong"


def test_score_band_moderate_at_60() -> None:
    assert _score_band(60) == "Moderate"


def test_score_band_weak_at_30() -> None:
    assert _score_band(30) == "Weak"


def test_score_band_none_returns_empty() -> None:
    """When score isn't known yet, render no band so the template's
    `{{ score_band }}` placeholder substitutes to empty without 'None'."""
    assert _score_band(None) == ""
