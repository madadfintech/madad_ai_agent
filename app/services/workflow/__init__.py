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
from .dispatcher import OnboardingDispatcher
from .mcp_identity import McpMadadIdentityClient
from .mcp_kyc import McpKycClient
from .onboarding import TEMPLATE_KEYS, OnboardingWorkflow
from .ports import (
    InMemoryKycClient,
    InMemoryMadadIdentityClient,
    KycClient,
    MadadIdentityClient,
    Messenger,
    RecordingMessenger,
    RecordingReminders,
    Reminders,
)
from .state import OnboardingState

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
    # ports
    "Messenger",
    "MadadIdentityClient",
    "KycClient",
    "Reminders",
    # in-memory / recording fakes
    "RecordingMessenger",
    "InMemoryMadadIdentityClient",
    "InMemoryKycClient",
    "RecordingReminders",
    # MCP-backed adapters
    "McpMadadIdentityClient",
    "McpKycClient",
    # real adapters
    "CommunicationMessenger",
    "NudgeReminders",
]
