"""QA #5 (2026-06-09): tokens must not be persisted in LangGraph checkpoints.

Before the fix, ``state.access_token`` / ``refresh_token`` / ``onboarding_token``
landed in every checkpoint snapshot — a fresh-token-by-design field surface
that nonetheless gets stored as plaintext on every node tick. With
``Field(exclude=True)`` they are stripped from ``model_dump()`` (the path
the checkpoint serializer goes through) and re-minted on resume by
``_live_token``.
"""

from __future__ import annotations

from app.services.workflow.state import OnboardingState


def test_token_fields_excluded_from_model_dump() -> None:
    state = OnboardingState(
        identity="+97455500005",
        access_token="ACCESS_SECRET",
        refresh_token="REFRESH_SECRET",
        onboarding_token="ONBOARDING_SECRET",
        token_expires_at=1234567890,
    )
    dumped = state.model_dump()
    assert "access_token" not in dumped
    assert "refresh_token" not in dumped
    assert "onboarding_token" not in dumped
    assert "token_expires_at" not in dumped


def test_token_fields_excluded_from_model_dump_json() -> None:
    state = OnboardingState(
        identity="+97455500005",
        access_token="ACCESS_SECRET",
        refresh_token="REFRESH_SECRET",
    )
    serialized = state.model_dump_json()
    assert "ACCESS_SECRET" not in serialized
    assert "REFRESH_SECRET" not in serialized


def test_tokens_remain_readable_in_memory() -> None:
    """Within a single execution, the workflow still needs to read the
    tokens it minted earlier in the same node call. Only the persistence
    layer is scrubbed."""
    state = OnboardingState(
        identity="+97455500005",
        access_token="ACCESS_SECRET",
    )
    assert state.access_token == "ACCESS_SECRET"
