"""Enumerations for the communication service."""

from __future__ import annotations

from enum import StrEnum

# Locale is platform-wide; re-exported here for backwards-compatible imports.
from app.shared.i18n import DEFAULT_LOCALE, Locale

__all__ = [
    "MessageDirection",
    "MessageType",
    "MessageStatus",
    "can_transition",
    "Locale",
    "DEFAULT_LOCALE",
]


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageType(StrEnum):
    TEXT = "text"
    TEMPLATE = "template"  # rendered from a CMS template
    MEDIA = "media"  # carries attachment(s)
    SYSTEM = "system"  # internal/system notice


class MessageStatus(StrEnum):
    """Lifecycle status of a message.

    Inbound messages are created ``RECEIVED``. Outbound messages flow
    ``QUEUED -> SENDING -> SENT -> DELIVERED -> READ`` and may go ``FAILED``.
    """

    RECEIVED = "received"  # inbound
    QUEUED = "queued"  # outbound, awaiting dispatch
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


# Allowed outbound status transitions (RECEIVED is terminal for inbound).
_ALLOWED_TRANSITIONS: dict[MessageStatus, frozenset[MessageStatus]] = {
    MessageStatus.QUEUED: frozenset({MessageStatus.SENDING, MessageStatus.FAILED}),
    MessageStatus.SENDING: frozenset({MessageStatus.SENT, MessageStatus.FAILED}),
    MessageStatus.SENT: frozenset(
        {MessageStatus.DELIVERED, MessageStatus.READ, MessageStatus.FAILED}
    ),
    MessageStatus.DELIVERED: frozenset({MessageStatus.READ}),
    MessageStatus.READ: frozenset(),
    MessageStatus.FAILED: frozenset({MessageStatus.QUEUED}),  # re-queue for retry
    MessageStatus.RECEIVED: frozenset(),
}


def can_transition(src: MessageStatus, dst: MessageStatus) -> bool:
    if src == dst:
        return True
    return dst in _ALLOWED_TRANSITIONS.get(src, frozenset())


