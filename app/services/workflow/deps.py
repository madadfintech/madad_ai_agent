"""Wiring for the onboarding workflow runtime."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.core.config import settings as default_settings
from app.services.communication.deps import get_communication_service
from app.services.document.checklist import ChecklistProvider
from app.services.nudge.deps import get_nudge_service
from app.shared.mcp import get_mcp_client
from app.shared.workflow import WorkflowRuntime, build_runtime

from .adapters import CommunicationMessenger, NudgeReminders
from .dispatcher import ALL_BACKEND_EVENTS, OnboardingDispatcher
from .mcp_identity import McpMadadIdentityClient
from .mcp_invoices import McpInvoiceClient
from .mcp_kyc import McpKycClient
from .mcp_payments import McpMonetizationPaymentAdapter
from .onboarding import OnboardingWorkflow
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
    invoices: InvoiceClient | None = None,
    checklist: ChecklistProvider | None = None,
    runtime: WorkflowRuntime | None = None,
    dedupe: WebhookDedupe | None = None,
    allowed_event_types: frozenset[str] | set[str] | None = None,
    offers_debounce_seconds: float = 0.0,
    cms: Any = None,
) -> OnboardingPlatform:
    runtime = runtime or build_runtime()
    # Shared dedupe instance: dispatcher uses it for the 24h webhook event-id
    # window; the workflow reuses the same Redis SET NX EX primitive for the
    # invoice inflight guard (UAT 2026-06-19: slow OCR causes webhook
    # retries to race the still-running node).
    shared_dedupe = dedupe or InMemoryWebhookDedupe()
    # Resolve the identity client ONCE so the workflow and the dispatcher share
    # the SAME instance — the dispatcher uses it for cross-channel canonical-run
    # resolution (email→phone), the workflow for its onboarding lookups.
    identity_client = identity or InMemoryMadadIdentityClient()
    messenger_client = messenger or RecordingMessenger()
    workflow = OnboardingWorkflow(
        messenger=messenger_client,
        identity=identity_client,
        kyc=kyc or InMemoryKycClient(required_documents=DEFAULT_REQUIRED_DOCS),
        payments=payments or InMemoryMonetizationPaymentClient(),
        reminders=reminders or RecordingReminders(),
        invoices=invoices or InMemoryInvoiceClient(),
        checklist=checklist,
        dedupe=shared_dedupe,
        offers_debounce_seconds=offers_debounce_seconds,
        cms=cms,
    )
    runtime.register(workflow)
    dispatcher = OnboardingDispatcher(
        runtime,
        dedupe=shared_dedupe,
        allowed_event_types=allowed_event_types or ALL_BACKEND_EVENTS,
        identity=identity_client,
        messenger=messenger_client,
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
        # CMS-driven checklist — same trigger as the CMS template provider:
        # postgres-persistence (== "we have a CMS store") OR mcp.enabled
        # (== "production wiring"). Operators edit the checklist via
        # ``POST /cms/checklists/onboarding.whatsapp.required_docs`` and the
        # next conversation picks it up within the cache TTL.
        from app.services.cms.deps import get_cms_service
        from app.services.document.checklist import CmsChecklistProvider

        cms_service = get_cms_service()
        checklist: ChecklistProvider = CmsChecklistProvider(cms_service)
        return build_onboarding_platform(
            messenger=CommunicationMessenger(get_communication_service()),
            identity=McpMadadIdentityClient(mcp),
            kyc=McpKycClient(mcp),
            payments=McpMonetizationPaymentAdapter(mcp),
            reminders=NudgeReminders(get_nudge_service()),
            invoices=McpInvoiceClient(mcp),
            checklist=checklist,
            dedupe=dedupe,
            # CMS handle on the workflow so the new ``_resolve_buttons``
            # helper can read CMS-stored reply-button labels (Ishan #2).
            # Hot-cached + pubsub-invalidated, so the agent picks up an
            # ops edit within ~60s.
            cms=cms_service,
            # NO debounce (user 2026-06-21): an offer message must reach the
            # SME the INSTANT each lender quotes — lenders can quote hours or
            # days apart, so coalescing hides/delays the first offer. The
            # ``_offers_sig`` guard already stops re-spamming the SAME set on
            # routine polls; 0 disables the coalesce window entirely so each
            # new lender's offer fires immediately.
            offers_debounce_seconds=0.0,
        )
    return build_onboarding_platform()
