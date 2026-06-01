"""Cross-service integration: CMS-driven schedules + delivery via Communication."""

from __future__ import annotations

from app.services.cms import CmsTemplateProvider, ConfigKind, build_cms_service
from app.services.communication import build_communication_service
from app.services.nudge import (
    CmsNudgeConfigProvider,
    CommunicationNotificationDispatcher,
    NudgeConfig,
    build_nudge_service,
)
from app.shared.i18n import Locale
from app.shared.workflow.enums import Channel

WA = Channel.WHATSAPP


async def test_schedule_sourced_from_cms():
    cms = build_cms_service()
    await cms.upsert(
        ConfigKind.NUDGE,
        "incomplete_docs",
        {
            "schedule": [
                {"offset": 0, "channels": ["whatsapp"], "template_key": "nudge.docs.1"},
            ],
            "max_attempts": 3,
        },
    )

    provider = CmsNudgeConfigProvider(cms)
    schedule = await provider.get_schedule("incomplete_docs")
    assert len(schedule.steps) == 1
    assert schedule.steps[0].template_key == "nudge.docs.1"
    assert schedule.steps[0].channels == [WA]


async def test_end_to_end_cms_nudge_communication():
    """A nudge renders a CMS template and delivers via the Communication service."""

    cms = build_cms_service()
    await cms.upsert_template(
        "nudge.docs.1",
        Locale.EN,
        "Hi! Please share your Audited Financial Statement.",
    )
    await cms.upsert(
        ConfigKind.NUDGE,
        "incomplete_docs",
        {"schedule": [{"offset": 0, "channels": ["whatsapp"], "template_key": "nudge.docs.1"}]},
    )

    comms = build_communication_service(template_provider=CmsTemplateProvider(cms))
    nudge = build_nudge_service(
        config_provider=CmsNudgeConfigProvider(cms),
        dispatcher=CommunicationNotificationDispatcher(comms),
        config=NudgeConfig(retry_jitter=False),
    )

    await nudge.start_sequence("incomplete_docs", {WA: "+97455500001"})
    processed = await nudge.run_due()

    assert len(processed) == 1
    # The message went all the way through Communication to its (stub) gateway.
    conversation = await comms.resolve_conversation(WA, "+97455500001")
    messages = await comms.get_messages(conversation.conversation_id)
    assert len(messages) == 1
    assert messages[0].text == "Hi! Please share your Audited Financial Statement."
