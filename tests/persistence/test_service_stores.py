"""PostgreSQL store adapters for CMS, communication, nudge, document, visibility
(verified on SQLite via the shared ``db`` fixture)."""

from __future__ import annotations

from datetime import timedelta

from app.shared.i18n import Locale
from app.shared.workflow.enums import Channel
from app.shared.workflow.utils import utcnow

# -- CMS config store (versioned) --------------------------------------------


async def test_cms_config_versioning_and_rollback_data(db):
    from app.services.cms.db import PostgresConfigStore
    from app.services.cms.enums import ConfigKind
    from app.services.cms.models import ConfigKey

    store = PostgresConfigStore(db)
    key = ConfigKey(ConfigKind.SETTING, "flag")

    v1 = await store.save_new_version(key, {"on": False})
    v2 = await store.save_new_version(key, {"on": True})
    assert (v1.version, v2.version) == (1, 2)

    current = await store.get_current(key)
    assert current.version == 2 and current.value == {"on": True}
    assert [v.version for v in await store.list_versions(key)] == [1, 2]
    assert (await store.get_version(key, 1)).value == {"on": False}
    assert [(k.kind, k.name) for k in await store.list_keys(ConfigKind.SETTING)] == [
        (ConfigKind.SETTING, "flag")
    ]


async def test_cms_template_key_channel_locale(db):
    from app.services.cms.db import PostgresConfigStore
    from app.services.cms.enums import ConfigKind
    from app.services.cms.models import ConfigKey

    store = PostgresConfigStore(db)
    key = ConfigKey(ConfigKind.TEMPLATE, "welcome", Channel.WHATSAPP, Locale.AR)
    await store.save_new_version(key, {"body": "مرحبا"})
    got = await store.get_current(key)
    assert got is not None and got.value["body"] == "مرحبا"
    # A different locale is a different key.
    other = ConfigKey(ConfigKind.TEMPLATE, "welcome", Channel.WHATSAPP, Locale.EN)
    assert await store.get_current(other) is None


# -- communication stores ----------------------------------------------------


async def test_communication_message_and_conversation(db):
    from app.services.communication.db import (
        PostgresConversationStore,
        PostgresMessageStore,
    )
    from app.services.communication.enums import MessageDirection, MessageStatus
    from app.services.communication.models import Conversation, Message

    conversations = PostgresConversationStore(db)
    conv = Conversation(channel=Channel.WHATSAPP, identity="+97455500001")
    await conversations.save(conv)
    assert (
        await conversations.find_open(Channel.WHATSAPP, "+97455500001")
    ).conversation_id == conv.conversation_id

    messages = PostgresMessageStore(db)
    msg = Message(
        conversation_id=conv.conversation_id,
        channel=Channel.WHATSAPP,
        identity="+97455500001",
        direction=MessageDirection.OUTBOUND,
        status=MessageStatus.SENT,
        provider_message_id="prov-1",
        text="hi",
    )
    await messages.create(msg)
    assert (await messages.get(msg.message_id)).text == "hi"
    assert (await messages.get_by_provider_id("prov-1")).message_id == msg.message_id
    assert len(await messages.list_by_conversation(conv.conversation_id)) == 1


# -- nudge stores ------------------------------------------------------------


async def test_nudge_sequences_and_due_reminders(db):
    from app.services.nudge.db import PostgresNudgeStore
    from app.services.nudge.models import NudgeSequence, Reminder

    store = PostgresNudgeStore(db)
    seq = NudgeSequence(
        reason="incomplete_docs", target_ref="APP-1", targets={Channel.WHATSAPP: "+974"}
    )
    await store.create_sequence(seq)
    assert (
        await store.find_active_sequence("incomplete_docs", "APP-1")
    ).sequence_id == seq.sequence_id
    assert len(await store.list_active_sequences(identity="+974")) == 1

    now = utcnow()
    due = Reminder(
        sequence_id=seq.sequence_id,
        step_index=0,
        scheduled_for=now - timedelta(minutes=1),
        template_key="t",
    )
    later = Reminder(
        sequence_id=seq.sequence_id,
        step_index=1,
        scheduled_for=now + timedelta(hours=1),
        template_key="t",
    )
    await store.create_reminder(due)
    await store.create_reminder(later)
    ready = await store.list_due(now)
    assert [r.reminder_id for r in ready] == [due.reminder_id]
    assert len(await store.list_by_sequence(seq.sequence_id)) == 2


# -- document stores ---------------------------------------------------------


async def test_document_and_batch_stores(db):
    from app.services.document.db import PostgresBatchStore, PostgresDocumentStore
    from app.services.document.enums import DocumentStatus
    from app.services.document.models import DocumentBatch, DocumentRecord

    batches = PostgresBatchStore(db)
    batch = DocumentBatch(application_ref="APP-1", checklist="onboarding")
    await batches.create(batch)
    assert (await batches.get(batch.batch_id)).checklist == "onboarding"

    documents = PostgresDocumentStore(db)
    doc = DocumentRecord(
        application_ref="APP-1",
        batch_id=batch.batch_id,
        filename="Trade.pdf",
        status=DocumentStatus.COMPLETED,
        document_type="trade_license",
        madad_ref="madoc-1",
    )
    await documents.create(doc)
    assert (await documents.get(doc.document_id)).madad_ref == "madoc-1"
    assert len(await documents.list_by_batch(batch.batch_id)) == 1
    assert len(await documents.list_by_application("APP-1")) == 1
    # Sovereignty: persisted JSON carries no document bytes / extracted fields.
    assert "fields" not in (await documents.get(doc.document_id)).model_dump()


# -- visibility activity store -----------------------------------------------


async def test_visibility_activity_query(db):
    from app.services.visibility.db import PostgresActivityStore
    from app.services.visibility.enums import ActivitySource
    from app.services.visibility.models import ActivityEvent
    from app.services.visibility.persistence import ActivityFilter

    store = PostgresActivityStore(db)
    await store.append(
        ActivityEvent(source=ActivitySource.WORKFLOW, type="workflow.run.started", run_id="r1")
    )
    await store.append(
        ActivityEvent(
            source=ActivitySource.COMMUNICATION,
            type="communication.message.sent",
            conversation_id="c1",
        )
    )

    assert len(await store.all()) == 2
    wf = await store.query(ActivityFilter(source=ActivitySource.WORKFLOW))
    assert len(wf) == 1 and wf[0].run_id == "r1"
    by_conv = await store.query(ActivityFilter(conversation_id="c1"))
    assert len(by_conv) == 1


async def test_visibility_query_paginates_in_sql(db):
    from app.services.visibility.db import PostgresActivityStore
    from app.services.visibility.enums import ActivitySource
    from app.services.visibility.models import ActivityEvent
    from app.services.visibility.persistence import ActivityFilter

    store = PostgresActivityStore(db)
    for i in range(5):
        await store.append(
            ActivityEvent(source=ActivitySource.WORKFLOW, type=f"e{i}", run_id="r")
        )

    # No-text path: offset/limit applied in SQL, ordered by occurred_at.
    page = await store.query(ActivityFilter(run_id="r"), limit=2, offset=1)
    assert [a.type for a in page] == ["e1", "e2"]

    # Text path: substring filter over type/summary still works.
    hit = await store.query(ActivityFilter(text="e3"))
    assert [a.type for a in hit] == ["e3"]
