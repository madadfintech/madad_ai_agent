"""Wiring for the onboarding workflow runtime."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.shared.workflow import WorkflowRuntime, build_runtime

from .dispatcher import OnboardingDispatcher
from .onboarding import OnboardingWorkflow
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


@dataclass
class OnboardingPlatform:
    """Everything needed to drive onboarding: runtime, workflow, dispatcher."""

    runtime: WorkflowRuntime
    workflow: OnboardingWorkflow
    dispatcher: OnboardingDispatcher


# Default admin-requested documents used by the in-memory KYC fake — the real
# list lives in the Madad backend and is fetched per run via
# ``madad_kyc_get_admin_requested_documents``.
DEFAULT_REQUIRED_DOCS = ["trade_license", "tax_card", "bank_statement"]


def build_onboarding_platform(
    *,
    messenger: Messenger | None = None,
    identity: MadadIdentityClient | None = None,
    kyc: KycClient | None = None,
    reminders: Reminders | None = None,
    runtime: WorkflowRuntime | None = None,
) -> OnboardingPlatform:
    runtime = runtime or build_runtime()
    workflow = OnboardingWorkflow(
        messenger=messenger or RecordingMessenger(),
        identity=identity or InMemoryMadadIdentityClient(),
        kyc=kyc or InMemoryKycClient(required_documents=DEFAULT_REQUIRED_DOCS),
        reminders=reminders or RecordingReminders(),
    )
    runtime.register(workflow)
    dispatcher = OnboardingDispatcher(runtime)
    return OnboardingPlatform(runtime=runtime, workflow=workflow, dispatcher=dispatcher)


@lru_cache(maxsize=1)
def get_onboarding_platform() -> OnboardingPlatform:
    """Process-singleton platform for the FastAPI app.

    Defaults to in-memory adapters for the onboarding business ports
    (MadadIdentityClient, KycClient). The MCP-backed replacements ship via
    ``McpMadadIdentityClient`` (Phase 1) and ``McpKycClient`` (Phase 2);
    operators wire them through ``build_onboarding_platform(...)`` once
    ``settings.mcp.enabled`` flips on in staging / production.
    """

    return build_onboarding_platform()
