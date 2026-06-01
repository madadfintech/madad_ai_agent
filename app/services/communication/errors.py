"""Communication service exception hierarchy."""

from __future__ import annotations

from app.core.exceptions import AppError


class CommunicationError(AppError):
    """Base class for communication service errors."""

    code = "communication_error"


class GatewayError(CommunicationError):
    """A transient failure dispatching a message through the MCP gateway.

    Raised by gateway adapters for retryable conditions (timeouts, upstream 5xx).
    The service retries these with backoff before marking a message failed.
    """

    code = "communication_gateway_error"


class TemplateNotFoundError(CommunicationError):
    code = "template_not_found"


class MissingTemplateVariableError(CommunicationError):
    code = "missing_template_variable"


class MessageNotFoundError(CommunicationError):
    code = "message_not_found"


class ConversationNotFoundError(CommunicationError):
    code = "conversation_not_found"


class InvalidMessageStatusError(CommunicationError):
    """An illegal message-status transition was attempted."""

    code = "invalid_message_status"
