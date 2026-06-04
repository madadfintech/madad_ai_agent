"""InMemoryMadadIdentityClient — the fake satisfying the new identity port."""

from __future__ import annotations

import pytest

from app.services.workflow.ports import (
    ContactCheckResult,
    InMemoryMadadIdentityClient,
    MadadIdentityClient,
)
from app.shared.workflow.enums import Channel

WA = Channel.WHATSAPP
EMAIL = Channel.EMAIL


def test_in_memory_satisfies_protocol() -> None:
    assert isinstance(InMemoryMadadIdentityClient(), MadadIdentityClient)


# -- open_session: existing user vs new lead ----------------------------------


async def test_open_session_returns_existing_user_with_access_token_for_known_phone() -> None:
    client = InMemoryMadadIdentityClient(known_phones={"+97455500001": "user_42"})
    s = await client.open_session(channel=WA, identifier="+97455500001")
    assert s.session_type == "existing_user"
    assert s.access_token is not None
    assert s.onboarding_token is None
    assert s.user_or_lead_ref == "user_42"
    assert s.refresh_token is not None  # refresh token issued alongside access


async def test_open_session_returns_existing_user_for_known_email() -> None:
    client = InMemoryMadadIdentityClient(known_emails={"sme@example.com": "user_7"})
    s = await client.open_session(channel=EMAIL, identifier="sme@example.com")
    assert s.session_type == "existing_user"
    assert s.user_or_lead_ref == "user_7"


async def test_open_session_returns_new_lead_for_unknown_identifier() -> None:
    client = InMemoryMadadIdentityClient()
    s = await client.open_session(channel=WA, identifier="+97499900000")
    assert s.session_type == "new_lead"
    assert s.access_token is None
    assert s.onboarding_token is not None
    assert s.user_or_lead_ref is not None


async def test_open_session_respects_create_onboarding_token_flag() -> None:
    client = InMemoryMadadIdentityClient()
    s = await client.open_session(
        channel=WA, identifier="+97499900001", create_onboarding_token=False
    )
    assert s.session_type == "new_lead"
    assert s.onboarding_token is None


# -- check_contact: the Q8 three-way branch -----------------------------------


@pytest.mark.parametrize(
    "phone,email,expected",
    [
        # Known phone → existing.
        ("+97455500001", None, ContactCheckResult(exists=True, field="phone")),
        # Known email → existing.
        (None, "sme@example.com", ContactCheckResult(exists=True, field="email")),
        # Unknown email on a fresh domain → can onboard.
        (None, "founder@startup.qa", ContactCheckResult(exists=False, domain_exists=False)),
        # Unknown email on a known-blocked domain → block + surface owner.
        (
            None,
            "newhire@blocked.qa",
            ContactCheckResult(
                exists=False, domain_exists=True, domain="blocked.qa"
            ),
        ),
        # Unknown phone, no email → onboard.
        ("+97499900001", None, ContactCheckResult(exists=False, domain_exists=False)),
    ],
)
async def test_check_contact_three_way(
    phone: str | None, email: str | None, expected: ContactCheckResult
) -> None:
    client = InMemoryMadadIdentityClient(
        known_phones={"+97455500001": "u1"},
        known_emails={"sme@example.com": "u2"},
        blocked_domains={"blocked.qa": "BlockedCo LLC"},
    )
    result = await client.check_contact(phone=phone, email=email)
    assert result.exists == expected.exists
    assert result.field == expected.field
    assert result.domain_exists == expected.domain_exists
    assert result.domain == expected.domain


# -- complete_onboarding: lead → known user -----------------------------------


async def test_complete_onboarding_promotes_a_lead_to_a_known_user() -> None:
    client = InMemoryMadadIdentityClient()
    # First contact: lead.
    s = await client.open_session(channel=WA, identifier="+97499900100")
    assert s.session_type == "new_lead"
    assert s.onboarding_token is not None

    # Complete onboarding → backend creates the user record.
    result = await client.complete_onboarding(
        first_name="Aisha",
        last_name="Karim",
        onboarding_token=s.onboarding_token,
        phone_number="+97499900100",
    )
    assert result["user"]["firstName"] == "Aisha"

    # Second channel-session call now returns existing_user with an access_token.
    s2 = await client.open_session(channel=WA, identifier="+97499900100")
    assert s2.session_type == "existing_user"
    assert s2.access_token is not None


# -- me / refresh / logout ---------------------------------------------------


async def test_me_returns_configured_journey_status() -> None:
    client = InMemoryMadadIdentityClient(journey_status="PRE_QUALIFIED")
    info = await client.me(access_token="any-token")
    assert info["user"]["journeyStatus"] == "PRE_QUALIFIED"


async def test_refresh_issues_a_new_token_pair() -> None:
    client = InMemoryMadadIdentityClient()
    tokens = await client.refresh(refresh_token="old-rt")
    assert tokens.access_token
    assert tokens.refresh_token
    assert tokens.access_token != "old-rt"


async def test_logout_then_me_with_same_token_fails() -> None:
    client = InMemoryMadadIdentityClient()
    await client.logout(access_token="tok-a")
    with pytest.raises(RuntimeError):
        await client.me(access_token="tok-a")


async def test_calls_recorded_for_introspection() -> None:
    client = InMemoryMadadIdentityClient()
    await client.open_session(channel=WA, identifier="+97499900001")
    await client.check_contact(phone="+97499900001")
    assert [name for name, _ in client.calls] == ["open_session", "check_contact"]
