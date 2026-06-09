"""OnboardingState — Phase 1 identity / journey / payment / idempotency fields."""

from __future__ import annotations

from datetime import UTC, datetime

from app.services.workflow.ports import ChannelSession, ContactCheckResult
from app.services.workflow.state import (
    JourneyStatus,
    OnboardingState,
)


def test_default_state_has_all_new_fields_at_none_or_empty() -> None:
    state = OnboardingState()
    # Identity defaults.
    assert state.channel_identity is None
    assert state.channel_session_response is None
    assert state.session_type is None
    assert state.access_token is None
    assert state.onboarding_token is None
    assert state.refresh_token is None
    assert state.token_expires_at is None
    assert state.madad_user_id is None
    assert state.check_contact_result is None
    assert state.domain_block_reason is None
    # Journey defaults.
    assert state.journey_status is None
    assert state.last_polled_at is None
    assert state.last_status_source is None
    # Payment defaults.
    assert state.business_details_id is None
    assert state.payment_product_id is None
    assert state.payment_id is None
    assert state.payment_link is None
    assert state.payment_status is None
    # Idempotency.
    assert state.idempotency_keys == {}


def test_state_carries_a_channel_session_for_existing_user() -> None:
    session = ChannelSession(
        session_type="existing_user",
        access_token="AT-1",
        refresh_token="RT-1",
        user_or_lead_ref="user_42",
    )
    state = OnboardingState(
        channel_identity="+97455500001",
        channel_session_response=session,
        session_type="existing_user",
        access_token=session.access_token,
        refresh_token=session.refresh_token,
        madad_user_id=session.user_or_lead_ref,
    )
    assert state.session_type == "existing_user"
    assert state.channel_session_response is session
    assert state.madad_user_id == "user_42"


def test_state_carries_contact_check_for_the_domain_blocked_branch() -> None:
    result = ContactCheckResult(
        exists=False, domain_exists=True, domain="blocked.qa"
    )
    state = OnboardingState(
        check_contact_result=result, domain_block_reason="blocked.qa"
    )
    assert state.check_contact_result is result
    assert state.domain_block_reason == "blocked.qa"


def test_state_holds_journey_status_and_poll_metadata() -> None:
    now = datetime.now(UTC)
    state = OnboardingState(
        journey_status=JourneyStatus.PRE_QUALIFIED,
        last_polled_at=now,
        last_status_source="poll",
    )
    assert state.journey_status is JourneyStatus.PRE_QUALIFIED
    assert state.last_polled_at == now
    assert state.last_status_source == "poll"


def test_payment_block_fields_carry_through_state() -> None:
    state = OnboardingState(
        business_details_id="biz-1",
        payment_product_id="prod-monetization",
        payment_id="pay-99",
        payment_link="https://pay.tess/x",
        payment_status="PENDING",
    )
    assert state.payment_id == "pay-99"
    assert state.payment_link == "https://pay.tess/x"
    assert state.payment_status == "PENDING"


def test_idempotency_keys_dict_persists_per_tool() -> None:
    state = OnboardingState(
        idempotency_keys={
            "madad_payments_create_monetization_payment": "run-1:create_monetization_payment",
            "madad_payments_send_monetization_payment_link": "run-1:send_monetization_payment_link",
        }
    )
    assert (
        state.idempotency_keys["madad_payments_create_monetization_payment"]
        == "run-1:create_monetization_payment"
    )
    assert len(state.idempotency_keys) == 2


def test_state_serialises_round_trip_through_model_dump_and_model_validate() -> None:
    # LangGraph's checkpointer round-trips state through model_dump+model_validate;
    # any new field that breaks this would silently lose data on resume.
    original = OnboardingState(
        channel_identity="+97455500001",
        session_type="new_lead",
        onboarding_token="OT-1",
        journey_status=JourneyStatus.SIGN_UP,
        idempotency_keys={"madad_payments_create_monetization_payment": "k1"},
    )
    restored = OnboardingState.model_validate(original.model_dump(mode="json"))
    assert restored.channel_identity == "+97455500001"
    assert restored.session_type == "new_lead"
    # QA #5 (2026-06-09): onboarding_token is now ``Field(exclude=True)``
    # so it does NOT survive the dump→reload cycle (the whole point — we
    # don't want tokens persisted in checkpoints). ``_live_token`` mints
    # a fresh one on the next node entry from the verified channel
    # identity, so workflow correctness is unchanged.
    assert restored.onboarding_token is None
    assert restored.journey_status is JourneyStatus.SIGN_UP
    assert restored.idempotency_keys == {
        "madad_payments_create_monetization_payment": "k1"
    }
