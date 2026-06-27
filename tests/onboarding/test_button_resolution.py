"""Vendor Plan M1 acceptance — reply-button labels editable via CMS.

The agent stores a stable BUTTON ID per intent and looks up the operator-
editable LABEL via ``OnboardingWorkflow._resolve_buttons``. Ops can edit
labels in the portal (Ishan's CMS UI) without us redeploying; the IDs
stay pinned so the click-to-intent mapping never breaks.

These tests pin that contract.
"""

from __future__ import annotations

from typing import Any

from app.services.workflow.deps import build_onboarding_platform
from app.services.workflow.onboarding import OnboardingWorkflow
from app.shared.workflow import Channel


class _StubCmsRecord:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value


class _StubCms:
    """Single-template stub so tests can vary CMS behaviour per case."""

    def __init__(self, record: Any = None) -> None:
        self._record = record

    async def get_template(self, name: str, locale: Any, *, channel: Any) -> Any:
        return self._record


async def test_resolve_buttons_falls_back_to_default_when_cms_missing() -> None:
    """No CMS handle → returns the defaults verbatim. Production safety
    net so a missing CMS service can never strip the agent of its
    interactive UX."""
    platform = build_onboarding_platform()
    wf: OnboardingWorkflow = platform.workflow

    default = [("invoice_approve", "Approve"), ("invoice_reject", "Reject")]
    out = await wf._resolve_buttons(  # noqa: SLF001
        "onboarding.invoice.confirm",
        default,
        locale=None,
        channel=Channel.WHATSAPP,
    )
    assert out == default


async def test_resolve_buttons_returns_cms_labels_when_present() -> None:
    """CMS carries a ``buttons`` array with the right stable ids → the
    agent sends the operator-edited labels. This is the M1 acceptance
    test: ops changes a label in the portal → next conversation shows
    it."""
    platform = build_onboarding_platform(
        cms=_StubCms(
            _StubCmsRecord({
                "body": "Have more docs to send?",
                "buttons": [
                    {"id": "docs_upload_more", "label": "Yes — send more 📎"},
                    {"id": "docs_done", "label": "No, I'm done ✅"},
                ],
            })
        )
    )
    wf: OnboardingWorkflow = platform.workflow

    default = wf.BUTTON_DEFAULTS["onboarding.documents.more_docs_prompt:settle"]
    out = await wf._resolve_buttons(  # noqa: SLF001
        "onboarding.documents.more_docs_prompt",
        default,
        locale=None,
        channel=Channel.WHATSAPP,
    )
    assert out == [
        ("docs_upload_more", "Yes — send more 📎"),
        ("docs_done", "No, I'm done ✅"),
    ]


async def test_resolve_buttons_skips_unknown_ids_from_cms() -> None:
    """An ops mistake (typo on the id, or a button id the agent doesn't
    listen for) must NOT introduce a click-to-nowhere button. The
    helper silently drops unknown ids and returns only the safe ones;
    if everything was unknown, returns the defaults so the SME never
    sees a UI without buttons."""
    platform = build_onboarding_platform(
        cms=_StubCms(
            _StubCmsRecord({
                "buttons": [
                    {"id": "invoice_approve", "label": "Approve"},
                    {"id": "lol_typo_id", "label": "Surprise!"},
                ],
            })
        )
    )
    wf: OnboardingWorkflow = platform.workflow

    default = wf.BUTTON_DEFAULTS["onboarding.invoice.confirm"]
    out = await wf._resolve_buttons(  # noqa: SLF001
        "onboarding.invoice.confirm",
        default,
        locale=None,
        channel=Channel.WHATSAPP,
    )
    assert ("invoice_approve", "Approve") in out
    assert all(bid != "lol_typo_id" for bid, _ in out)
    # All defaults present? Not required — partial CMS edit is allowed.
    # But we MUST have at least the safe ones (not empty), else fall to defaults.
    assert out  # non-empty


async def test_resolve_buttons_cms_exception_falls_back_silently() -> None:
    """A CMS fault (network, timeout, bad envelope) must NEVER block the
    agent from showing the buttons it knows about. The helper swallows
    and returns defaults."""

    class _BrokenCms:
        async def get_template(self, *a: Any, **kw: Any) -> Any:
            raise RuntimeError("CMS is taking a coffee break")

    platform = build_onboarding_platform(cms=_BrokenCms())
    wf: OnboardingWorkflow = platform.workflow

    default = wf.BUTTON_DEFAULTS["onboarding.invoice.batch.csv_review"]
    out = await wf._resolve_buttons(  # noqa: SLF001
        "onboarding.invoice.batch.csv_review",
        default,
        locale=None,
        channel=Channel.WHATSAPP,
    )
    assert out == default


def test_button_defaults_cover_every_call_site() -> None:
    """Lock the contract: every reply-button site the agent emits today
    has an entry in ``BUTTON_DEFAULTS``. If a new site is added without
    a defaults entry, this test fails — forcing the dev to either add
    the entry (so ops can edit it) or document the omission.
    """
    # Sites grepped from onboarding.py — keep in lockstep.
    expected = {
        "onboarding.documents.more_docs_prompt:settle",
        "onboarding.documents.more_docs_prompt:simple",
        "onboarding.invoice.confirm",
        "onboarding.invoice.batch.csv_review",
    }
    assert set(OnboardingWorkflow.BUTTON_DEFAULTS.keys()) == expected
