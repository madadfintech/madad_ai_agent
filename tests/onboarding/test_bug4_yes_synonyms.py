"""Bug #4 (2026-06-09): bot rejected 'Ok' / 'Sure' at the campaign YES gate.

QA (Musab): "Bot expects exact answer (e.g. Cant say Ok instead of Yes),
Buttons would be better to avoid this." Expanded YES/NO synonym sets cover
the common conversational affirmations + negations without requiring an
interactive button rollout. Buttons still go in later (CTA path); this is
the surgical first cut.
"""

from __future__ import annotations

import pytest

from app.services.workflow.state import clean_email_quoted_reply, is_no, is_yes
from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455500401"


@pytest.mark.parametrize(
    "text",
    [
        "Yes",
        "YES",
        "y",
        "yeah",
        "Yep",
        "yup",
        "Ok",
        "OK",
        "okay",
        "k",
        "Sure",
        "sure",
        "Absolutely",
        "definitely",
        "fine",
        "alright",
        "Go ahead",
        "Lets go",
        "let's go",
        "Sounds good",
        "Continue",
        "agreed",
    ],
)
def test_expanded_yes_synonyms(text: str) -> None:
    assert is_yes(text), f"{text!r} should be recognised as YES"


@pytest.mark.parametrize(
    "text",
    [
        "No",
        "NO",
        "n",
        "nope",
        "Nah",
        "not now",
        "not interested",
        "stop",
        "Cancel",
        "Decline",
    ],
)
def test_expanded_no_synonyms(text: str) -> None:
    assert is_no(text), f"{text!r} should be recognised as NO"


def test_gmail_quoted_yes_reply_is_recognised() -> None:
    text = (
        "YES\n\n"
        "On Sun, Jul 5, 2026 at 3:19 PM Madad Support wrote:\n"
        "> Are you interested in financing for your business?\n"
        "> Please reply YES or NO."
    )

    assert clean_email_quoted_reply(text) == "YES"
    assert is_yes(text)


async def test_campaign_accepts_ok_as_yes(make_harness) -> None:
    """The YES → consent_cr edge fires when the SME replies 'Ok' instead
    of the literal 'YES'."""
    harness = make_harness(known_phones={IDENTITY: "user_42"})
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})

    result = await runtime.resume(WA, IDENTITY, message={"text": "Ok"})

    assert result.prompt == {"waiting_for": "upload", "step": "consent_cr"}
    assert "onboarding.consent.request" in harness.messenger.templates()
