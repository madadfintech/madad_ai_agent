"""MADAD Communication Service.

Centralized communication orchestration over WhatsApp and Email. Handles
inbound/outbound messaging, conversation threading, template rendering
(multilingual), attachment metadata, delivery tracking, retry, events, and audit.

External channel APIs are NOT called here — outbound dispatch goes through the
external MCP cluster via the ``CommunicationGateway`` port, and inbound arrives
pre-normalized from the MCP layer.
"""

from __future__ import annotations

from app.shared.mcp import MCPToolCaller  # re-exported for backward compat

from .audit import CommunicationAuditEntry, CommunicationAuditLogger
from .deps import build_communication_service, get_communication_service
from .dispatcher import ConversationDispatcher, NoopConversationDispatcher
from .enums import (
    DEFAULT_LOCALE,
    Locale,
    MessageDirection,
    MessageStatus,
    MessageType,
    can_transition,
)
from .errors import (
    CommunicationError,
    ConversationNotFoundError,
    GatewayError,
    InvalidMessageStatusError,
    MessageNotFoundError,
    MissingTemplateVariableError,
    TemplateNotFoundError,
)
from .events import (
    CommunicationEvent,
    CommunicationEventBus,
    CommunicationEventType,
    InMemoryCommunicationEventBus,
)
from .gateway import (
    CommunicationGateway,
    InMemoryCommunicationGateway,
    McpCommunicationGateway,
    OutboundDispatchResult,
)
from .models import Attachment, Conversation, Message
from .persistence import (
    ConversationStore,
    InMemoryConversationStore,
    InMemoryMessageStore,
    MessageStore,
)
from .schemas import (
    AttachmentDTO,
    DeliveryStatusUpdate,
    InboundMessageDTO,
    MessageDTO,
    OutboundMessageRequest,
)
from .service import CommunicationConfig, CommunicationService
from .templating import (
    InMemoryTemplateProvider,
    Template,
    TemplateProvider,
    TemplateRenderer,
    render,
)

__all__ = [
    # service
    "CommunicationService",
    "CommunicationConfig",
    "build_communication_service",
    "get_communication_service",
    # enums
    "MessageDirection",
    "MessageType",
    "MessageStatus",
    "Locale",
    "DEFAULT_LOCALE",
    "can_transition",
    # models
    "Message",
    "Conversation",
    "Attachment",
    # DTOs
    "InboundMessageDTO",
    "OutboundMessageRequest",
    "DeliveryStatusUpdate",
    "MessageDTO",
    "AttachmentDTO",
    # gateway
    "CommunicationGateway",
    "InMemoryCommunicationGateway",
    "McpCommunicationGateway",
    "MCPToolCaller",
    "OutboundDispatchResult",
    # templating
    "TemplateProvider",
    "InMemoryTemplateProvider",
    "TemplateRenderer",
    "Template",
    "render",
    # persistence
    "MessageStore",
    "ConversationStore",
    "InMemoryMessageStore",
    "InMemoryConversationStore",
    # events
    "CommunicationEvent",
    "CommunicationEventType",
    "CommunicationEventBus",
    "InMemoryCommunicationEventBus",
    # audit
    "CommunicationAuditLogger",
    "CommunicationAuditEntry",
    # dispatcher
    "ConversationDispatcher",
    "NoopConversationDispatcher",
    # errors
    "CommunicationError",
    "GatewayError",
    "TemplateNotFoundError",
    "MissingTemplateVariableError",
    "MessageNotFoundError",
    "ConversationNotFoundError",
    "InvalidMessageStatusError",
]
