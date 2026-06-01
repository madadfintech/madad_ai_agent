"""Event-driven integration: real service events flow into the activity log."""

from __future__ import annotations

from app.services.communication import (
    InboundMessageDTO,
    InMemoryCommunicationEventBus,
    OutboundMessageRequest,
    build_communication_service,
)
from app.services.visibility import (
    CommunicationMessageSource,
    build_visibility_service,
    subscribe_communication,
)
from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455500001"


async def test_communication_events_feed_visibility_and_replay():
    # Wire: communication's event bus -> visibility recorder; replay pulls
    # message content from the communication service.
    bus = InMemoryCommunicationEventBus()
    comms = build_communication_service(events=bus)
    visibility = build_visibility_service(message_source=CommunicationMessageSource(comms))
    subscribe_communication(bus, visibility)

    await comms.ingest_inbound(InboundMessageDTO(channel=WA, identity=IDENTITY, text="YES"))
    await comms.send(
        OutboundMessageRequest(channel=WA, identity=IDENTITY, text="Welcome to Madad!")
    )

    # Activities were recorded from the real communication events.
    conversations = await visibility.list_conversations()
    assert len(conversations) == 1
    conversation_id = conversations[0].conversation_id

    # Replay merges the activity timeline with the actual message text.
    replay = await visibility.replay_conversation(conversation_id)
    assert replay.message_count == 2
    summaries = [e.summary for e in replay.entries]
    assert "YES" in summaries
    assert "Welcome to Madad!" in summaries

    metrics = visibility.get_metrics()
    assert metrics.by_source["communication"] >= 2
