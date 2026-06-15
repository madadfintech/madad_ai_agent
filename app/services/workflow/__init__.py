"""MADAD Conversational Workflow Service.

Hosts the business workflow graphs (Phase 1.a onboarding, Steps 1–8) that run on
the shared workflow runtime and orchestrate the other services through ports.
External integrations (WhatsApp/Email, OCR, Tess, Madad APIs) are consumed only
via MCP — never implemented here.
"""

from __future__ import annotations

from .adapters import CommunicationMessenger, NudgeReminders
from .deps import (
    OnboardingPlatform,
    build_onboarding_platform,
    get_onboarding_platform,
)
from .dispatcher import (
    ALL_BACKEND_EVENTS,
    PHASE1A_BACKEND_EVENTS,
    PHASE1B_BACKEND_EVENTS,
    OnboardingDispatcher,
    UnknownEventTypeError,
    translate_backend_event,
)
from .mcp_identity import McpMadadIdentityClient
from .mcp_invoices import McpInvoiceClient
from .mcp_kyc import McpKycClient
from .mcp_payments import McpMonetizationPaymentAdapter, McpTessLoanPaymentAdapter
from .onboarding import TEMPLATE_KEYS, OnboardingWorkflow
from .ports import (
    InMemoryInvoiceClient,
    InMemoryKycClient,
    InMemoryMadadIdentityClient,
    InMemoryMonetizationPaymentClient,
    InvoiceClient,
    KycClient,
    MadadIdentityClient,
    Messenger,
    MonetizationPaymentClient,
    RecordingMessenger,
    RecordingReminders,
    Reminders,
)
from .state import OnboardingState
from .webhook_dedupe import (
    InMemoryWebhookDedupe,
    RedisWebhookDedupe,
    WebhookDedupe,
)

__all__ = [
    # workflow + state
    "OnboardingWorkflow",
    "OnboardingState",
    "TEMPLATE_KEYS",
    # wiring
    "OnboardingPlatform",
    "build_onboarding_platform",
    "get_onboarding_platform",
    "OnboardingDispatcher",
    "UnknownEventTypeError",
    "translate_backend_event",
    "ALL_BACKEND_EVENTS",
    "PHASE1A_BACKEND_EVENTS",
    "PHASE1B_BACKEND_EVENTS",
    "WebhookDedupe",
    "InMemoryWebhookDedupe",
    "RedisWebhookDedupe",
    # ports
    "Messenger",
    "MadadIdentityClient",
    "KycClient",
    "MonetizationPaymentClient",
    "InvoiceClient",
    "Reminders",
    # in-memory / recording fakes
    "RecordingMessenger",
    "InMemoryMadadIdentityClient",
    "InMemoryKycClient",
    "InMemoryMonetizationPaymentClient",
    "InMemoryInvoiceClient",
    "RecordingReminders",
    # MCP-backed adapters
    "McpMadadIdentityClient",
    "McpKycClient",
    "McpMonetizationPaymentAdapter",
    "McpTessLoanPaymentAdapter",
    "McpInvoiceClient",
    # real adapters
    "CommunicationMessenger",
    "NudgeReminders",
]
