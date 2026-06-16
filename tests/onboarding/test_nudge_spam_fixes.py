"""UAT 2026-06-16 nudge-spam fixes — three behavioural guarantees.

Background: Madad's two test numbers (+919497191690 and +918287611995)
received the "Are you interested in financing for your business?"
prompt every minute, non-stop. RCA: the status-poller picked a stuck
old payment_await run as pollable, called ``dispatcher.resume_external
(channel, identity)``, which resumed the SESSION's ``active_run_id``
(the LATEST run, currently at campaign_await), and ``_campaign_await``
fired its default answer via ``_contextual_off_script`` on every poll.
Then every repeat test-reset stacked another run for the same identity
because ``forget-session`` was returning 401 (auth mismatch).

These tests pin the three structural fixes:

1. /workflow/campaign/start cancels any prior waiting runs for the
   same (channel, identity) before starting a fresh one.
2. The three text-input wait nodes (_campaign_await, _consent_await,
   _business_email_await) silently re-park when a synthetic resume
   (status_update / poll / docs_settle / phase1b_event) arrives,
   instead of firing their canned prompt.
3. ``settings.admin_api_token_extras`` is parsed from a comma-separated
   env var AND require_admin accepts any of the listed tokens.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.workflow.deps import get_onboarding_platform
from app.services.workflow.main import app
from app.shared.workflow import Channel
from app.shared.workflow.enums import RunStatus

WA = Channel.WHATSAPP


# ---- 1. campaign/start idempotency --------------------------------------

def test_campaign_start_cancels_previous_runs_for_same_identity() -> None:
    """Two successive ``/workflow/campaign/start`` calls leave only ONE
    waiting run for the identity — the second one cancels the first."""
    client = TestClient(app)
    identity = "+97455500C01"

    first = client.post(
        "/workflow/campaign/start",
        json={"channel": "whatsapp", "identity": identity},
    )
    assert first.status_code == 200
    first_id = first.json()["run_id"]

    second = client.post(
        "/workflow/campaign/start",
        json={"channel": "whatsapp", "identity": identity},
    )
    assert second.status_code == 200
    second_id = second.json()["run_id"]
    assert first_id != second_id

    # First run is now CANCELLED.
    platform = get_onboarding_platform()
    first_run = await_helper(platform.runtime.run_store.get(first_id))
    assert first_run.status == RunStatus.CANCELLED
    # Second is still active.
    second_run = await_helper(platform.runtime.run_store.get(second_id))
    assert second_run.status == RunStatus.WAITING_FOR_INPUT


def await_helper(coro):
    """Run a coroutine to completion in the test thread."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---- 2. wait-node synthetic-resume guards ------------------------------

async def test_campaign_await_silent_on_status_update(harness) -> None:
    """Synthetic status_update / poll / phase1b resume at campaign_await
    must NOT fire the "Are you interested in financing?" answer."""
    runtime = harness.platform.runtime
    identity = "+97455500C02"
    await runtime.start("onboarding", WA, identity, input={"trigger": "campaign"})
    # The intro fired naturally during start — count it.
    template_count_before = len(harness.messenger.templates())

    # Now simulate the poller waking the wrong run.
    await runtime.resume(WA, identity, message={
        "type": "status_update", "last_status_source": "poll",
    })
    await runtime.resume(WA, identity, message={
        "type": "phase1b_event", "event": "transaction.disbursed",
    })

    # No new templates fired — the node re-parked silently.
    assert len(harness.messenger.templates()) == template_count_before


async def test_consent_await_silent_on_status_update(harness) -> None:
    """Same guard at the CR/consent step — backend status_update or
    poller wake must not re-fire the CR prompt."""
    runtime = harness.platform.runtime
    identity = "+97455500C03"
    await runtime.start("onboarding", WA, identity, input={"trigger": "campaign"})
    await runtime.resume(WA, identity, message={"text": "YES"})
    await runtime.resume(WA, identity, message={"text": "biz@example.com"})
    # Now parked at consent_cr awaiting the CR upload.
    template_count_before = len(harness.messenger.templates())

    await runtime.resume(WA, identity, message={
        "type": "status_update", "last_status_source": "poll",
    })

    assert len(harness.messenger.templates()) == template_count_before


async def test_business_email_await_silent_on_status_update(harness) -> None:
    """Same guard at business_email_send/await — synthetic resume must
    not re-fire the email-request nag."""
    runtime = harness.platform.runtime
    identity = "+97455500C04"
    await runtime.start("onboarding", WA, identity, input={"trigger": "campaign"})
    await runtime.resume(WA, identity, message={"text": "YES"})
    # Now parked at business_email_await.
    template_count_before = len(harness.messenger.templates())

    await runtime.resume(WA, identity, message={
        "type": "status_update", "last_status_source": "poll",
    })

    assert len(harness.messenger.templates()) == template_count_before


# ---- 3. admin token extras list -----------------------------------------

def test_admin_extras_parsed_from_comma_separated_env() -> None:
    """``ADMIN_API_TOKEN_EXTRAS=dbwipe, other, third`` is parsed into
    a clean list. Operators don't have to write JSON in env files."""
    from app.core.config import Settings

    s = Settings(admin_api_token_extras_raw="dbwipe, other,  third ")
    assert s.admin_api_token_extras == ["dbwipe", "other", "third"]


def test_admin_extras_accepts_json_list_form() -> None:
    """Back-compat: a JSON list string also parses correctly."""
    from app.core.config import Settings

    s = Settings(admin_api_token_extras_raw='["a","b"]')
    assert s.admin_api_token_extras == ["a", "b"]


def test_admin_extras_empty_or_missing_yields_empty_list() -> None:
    """Defaults: when the env var is unset or empty, no extras."""
    from app.core.config import Settings

    assert Settings(admin_api_token_extras_raw=None).admin_api_token_extras == []
    assert Settings(admin_api_token_extras_raw="").admin_api_token_extras == []
    assert Settings(admin_api_token_extras_raw="   ").admin_api_token_extras == []


def test_require_admin_accepts_any_extra_token(monkeypatch) -> None:
    """Bearer that matches any extra token clears require_admin."""
    import asyncio

    from app.core import security
    from app.core.config import Settings
    from app.core.exceptions import AppError

    test_settings = Settings(
        admin_api_token="primary-token",
        admin_api_token_extras_raw="dbwipe",
    )
    monkeypatch.setattr(security, "settings", test_settings)

    class _Req:
        def __init__(self, bearer):
            self.url = type("U", (), {"path": "/workflow/admin/forget-session"})()
            self.headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}

    # Primary token works.
    asyncio.run(security.require_admin(_Req("primary-token")))  # type: ignore[arg-type]
    # Extra token works.
    asyncio.run(security.require_admin(_Req("dbwipe")))  # type: ignore[arg-type]
    # Unknown token is rejected.
    try:
        asyncio.run(security.require_admin(_Req("nope")))  # type: ignore[arg-type]
        raise AssertionError("expected unauthorised")
    except AppError as exc:
        assert exc.code == "unauthorized"
