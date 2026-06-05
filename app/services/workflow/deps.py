"""Wiring for the onboarding workflow runtime."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.core.config import settings as default_settings
from app.services.communication.deps import get_communication_service
from app.services.nudge.deps import get_nudge_service
from app.shared.mcp import get_mcp_client
from app.shared.workflow import WorkflowRuntime, build_runtime

from .adapters import CommunicationMessenger, NudgeReminders
from .dispatcher import ALL_BACKEND_EVENTS, OnboardingDispatcher
from .mcp_identity import McpMadadIdentityClient
from .mcp_kyc import McpKycClient
from .mcp_payments import McpMonetizationPaymentAdapter
from .onboarding import OnboardingWorkflow
from .ports import (
    InMemoryKycClient,
    InMemoryMadadIdentityClient,
    InMemoryMonetizationPaymentClient,
    KycClient,
    MadadIdentityClient,
    Messenger,
    MonetizationPaymentClient,
    RecordingMessenger,
    RecordingReminders,
    Reminders,
)
from .webhook_dedupe import InMemoryWebhookDedupe, RedisWebhookDedupe, WebhookDedupe


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
    payments: MonetizationPaymentClient | None = None,
    reminders: Reminders | None = None,
    runtime: WorkflowRuntime | None = None,
    dedupe: WebhookDedupe | None = None,
    allowed_event_types: frozenset[str] | set[str] | None = None,
) -> OnboardingPlatform:
    runtime = runtime or build_runtime()
    workflow = OnboardingWorkflow(
        messenger=messenger or RecordingMessenger(),
        identity=identity or InMemoryMadadIdentityClient(),
        kyc=kyc or InMemoryKycClient(required_documents=DEFAULT_REQUIRED_DOCS),
        payments=payments or InMemoryMonetizationPaymentClient(),
        reminders=reminders or RecordingReminders(),
    )
    runtime.register(workflow)
    dispatcher = OnboardingDispatcher(
        runtime,
        dedupe=dedupe or InMemoryWebhookDedupe(),
        allowed_event_types=allowed_event_types or ALL_BACKEND_EVENTS,
    )
    return OnboardingPlatform(runtime=runtime, workflow=workflow, dispatcher=dispatcher)


@lru_cache(maxsize=1)
def get_onboarding_platform() -> OnboardingPlatform:
    """Process-singleton platform for the FastAPI app.

    When ``settings.mcp.enabled`` is on, the agent's business ports are
    wired to the MCP-backed adapters (McpMadadIdentityClient, McpKycClient,
    McpMonetizationPaymentAdapter) — every workflow node calls the real
    Madad cluster. When MCP is off (dev / tests), the in-memory fakes serve
    as the defaults — the workflow runs end-to-end without any network.
    """

    if default_settings.mcp.enabled:
        mcp = get_mcp_client()
        # Use Redis-backed webhook dedupe when a Redis URL is configured —
        # otherwise restarts lose the 24h seen-event-id window and Madad's
        # backend retries could re-drive runs.
        dedupe: WebhookDedupe = (
            RedisWebhookDedupe(
                url=default_settings.redis.url,
                key_prefix=default_settings.redis.key_prefix,
            )
            if default_settings.redis.url
            else InMemoryWebhookDedupe()
        )
        return build_onboarding_platform(
            messenger=CommunicationMessenger(get_communication_service()),
            identity=McpMadadIdentityClient(mcp),
            kyc=McpKycClient(mcp),
            payments=McpMonetizationPaymentAdapter(mcp),
            reminders=NudgeReminders(get_nudge_service()),
            dedupe=dedupe,
        )
    return build_onboarding_platform()
