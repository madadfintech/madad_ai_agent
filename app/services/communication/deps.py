"""Dependency wiring for the communication service.

``build_communication_service`` assembles the service from adapters — in-memory
by default (tests/dev), or production adapters injected by the caller. The FastAPI
app uses a process-singleton built here.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from .audit import CommunicationAuditLogger
from .dispatcher import ConversationDispatcher
from .events import CommunicationEventBus, InMemoryCommunicationEventBus
from .gateway import CommunicationGateway, InMemoryCommunicationGateway
from .persistence import (
    ConversationStore,
    InMemoryConversationStore,
    InMemoryMessageStore,
    MessageStore,
)
from .service import CommunicationConfig, CommunicationService, SleepFn
from .templating import InMemoryTemplateProvider, TemplateProvider, TemplateRenderer


def build_communication_service(
    *,
    messages: MessageStore | None = None,
    conversations: ConversationStore | None = None,
    gateway: CommunicationGateway | None = None,
    template_provider: TemplateProvider | None = None,
    events: CommunicationEventBus | None = None,
    dispatcher: ConversationDispatcher | None = None,
    config: CommunicationConfig | None = None,
    sleep: SleepFn | None = None,
) -> CommunicationService:
    cfg = config or CommunicationConfig()
    provider = template_provider or InMemoryTemplateProvider()
    renderer = TemplateRenderer(provider, default_locale=cfg.default_locale)
    return CommunicationService(
        messages=messages or InMemoryMessageStore(),
        conversations=conversations or InMemoryConversationStore(),
        gateway=gateway or InMemoryCommunicationGateway(),
        renderer=renderer,
        events=events or InMemoryCommunicationEventBus(),
        audit=CommunicationAuditLogger(),
        dispatcher=dispatcher,
        config=cfg,
        sleep=sleep,
    )


@lru_cache(maxsize=1)
def get_communication_service() -> CommunicationService:
    """Process-singleton service; persistence + outbound gateway from settings."""

    from app.core.config import settings

    kwargs: dict[str, Any] = {}
    if settings.persistence.backend == "postgres":
        from app.shared.db.provider import get_database

        from .db import PostgresConversationStore, PostgresMessageStore

        db = get_database()
        kwargs["messages"] = PostgresMessageStore(db)
        kwargs["conversations"] = PostgresConversationStore(db)
    if settings.mcp.enabled:
        from app.shared.mcp import get_mcp_client

        from .gateway import McpCommunicationGateway

        kwargs["gateway"] = McpCommunicationGateway(get_mcp_client())
    return build_communication_service(**kwargs)
