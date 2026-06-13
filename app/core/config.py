"""Application configuration.

Settings are loaded from environment variables / ``.env``. Nested groups use a
double-underscore delimiter, e.g. ``REDIS__URL`` maps to ``settings.redis.url``.

Only the configuration the shared workflow runtime needs is defined here. Other
service-specific settings will be layered on in their own modules.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RedisSettings(BaseModel):
    """Redis connection + keyspace settings (sessions, events, scheduling)."""

    url: str = "redis://localhost:6379/0"
    key_prefix: str = "madad"
    # Stream used by the Redis event-bus adapter.
    event_stream: str = "stream:workflow"
    # Consumer group for workflow event consumers.
    event_group: str = "workflow-runtime"
    max_stream_len: int = 100_000


class PostgresSettings(BaseModel):
    """PostgreSQL settings for the LangGraph checkpointer and run store.

    The runtime persists ONLY orchestration state in the ``workflow`` schema.
    Business data lives in Madad's backend (accessed via their APIs).
    """

    dsn: str = "postgresql+asyncpg://madad:change_me@localhost:5432/madad"
    schema_name: str = "workflow"
    # SQLAlchemy connection pool: ``pool_size`` persistent connections + up to
    # ``max_overflow`` extra under load; recycle to avoid stale server-side conns.
    pool_size: int = 10
    max_overflow: int = 5

    @property
    def libpq_dsn(self) -> str:
        """DSN in plain libpq form (``postgresql://...``) — for libraries that
        speak psycopg directly rather than through SQLAlchemy (e.g. the
        langgraph Postgres checkpointer, or Alembic with the sync driver).

        Strips the SQLAlchemy ``+asyncpg`` / ``+psycopg`` dialect suffix that
        psycopg can't parse.
        """

        for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://"):
            if self.dsn.startswith(prefix):
                return "postgresql://" + self.dsn[len(prefix):]
        return self.dsn
    pool_recycle_seconds: int = 1800


class PersistenceSettings(BaseModel):
    """Selects in-memory (dev/tests) vs durable backends per concern.

    ``backend=postgres`` makes every service use its PostgreSQL store (over
    ``PostgresSettings.dsn``); ``cms_cache=redis`` enables cross-instance CMS
    cache invalidation. Defaults keep tests/dev fully in-memory.
    """

    backend: str = "memory"  # memory | postgres
    cms_cache: str = "memory"  # memory | redis


class CelerySettings(BaseModel):
    """Celery broker/result backend + beat schedule intervals (seconds).

    Periodic platform jobs run under Celery beat: draining due nudges, re-driving
    crash-interrupted workflow runs, and timing out lapsed waiting sessions.
    Dedicated Redis logical DBs (1 = broker, 2 = results) keep this traffic off
    the application keyspace (DB 0).
    """

    broker_url: str = "redis://localhost:6379/1"
    result_backend: str = "redis://localhost:6379/2"
    timezone: str = "UTC"

    # Beat intervals (seconds). Nudges tick frequently; recovery/sweeps are coarse.
    nudge_run_due_seconds: float = 60.0
    workflow_recover_seconds: float = 300.0
    workflow_timeout_sweep_seconds: float = 300.0
    # Beat-tick interval for the journey-status polling worker. Every tick
    # scans all WAITING runs and decides per-run whether a poll is due via
    # the cadence-by-status logic in :mod:`app.workers.status_poller`. A 60s
    # tick balances responsiveness against load (per-run cadence is 5 min
    # or longer, so 60s is "fast enough to catch the next due window").
    status_poller_seconds: float = 60.0


class EventBusSettings(BaseModel):
    """Unified cross-process event bus (Redis Streams in production).

    Each service keeps its in-process typed bus as the domain transport; this is
    the single cross-process stream that the Operational Visibility consumer
    reads. ``transport=memory`` keeps tests/dev fully in-process.
    """

    transport: str = "memory"  # memory | redis
    stream: str = "stream:events"
    group: str = "visibility"
    consumer: str = "visibility-1"
    max_len: int = 100_000
    block_ms: int = 5_000
    batch_size: int = 100


class McpSettings(BaseModel):
    """Shared MCP client config. The agentic platform consumes Madad/Tess/channel
    capabilities only through the MCP cluster (owned by a separate team).

    ``enabled=False`` (default) keeps every service on its in-memory gateway, so
    dev/tests need no MCP cluster. When enabled, services route through the real
    fastmcp client over Streamable HTTP. The deployed UAT endpoint is the Cloud
    Run URL; ``auth_mode`` selects bearer-token (UAT) or Cloud Run IAM ID-token
    (production). Tool names live in ``app.shared.mcp.registry``.
    """

    enabled: bool = False
    endpoint: str = "https://madad-mcp-cluster-626656664233.me-central1.run.app/mcp"
    transport: str = "streamable-http"  # informational; only one transport today
    protocol_version: str = "2025-06-18"  # MCP spec version we target

    # Auth seams.
    auth_mode: str = "bearer"  # bearer (UAT) | iam (Cloud Run prod)
    auth_token: str | None = None  # bearer token if auth_mode=bearer
    iam_audience: str | None = None  # Cloud Run audience for ID token (no /mcp suffix)
    signing_secret: str | None = None  # HMAC secret for the optional signed header
    signing_header: str = "X-Madad-Agent-Signature"
    ip_allowlist: list[str] = Field(default_factory=list)  # informational

    # Transport behaviour.
    timeout_seconds: float = 10.0
    retry_max_attempts: int = 1  # single-shot by default; tools must opt-in for retry
    retry_base_delay_seconds: float = 0.5
    retry_max_delay_seconds: float = 10.0
    # Tool names safe to retry transparently. Reads are always safe; payment write
    # tools are safe because Ishan now honours an ``idempotency_key`` parameter.
    idempotent_tools: set[str] = Field(default_factory=set)


class SecuritySettings(BaseModel):
    """Public-API auth seams. Both are dev-open until a secret is configured.

    ``jwt_*`` gate the service/public API (api.ai.madadfintech.com) with a bearer
    JWT; ``webhook_*`` verify the HMAC signature on Madad-platform callbacks
    (offer-acceptance + backend status). Exact schemes are confirmable with
    Madad later without touching route handlers.
    """

    jwt_secret: str | None = None
    jwt_algorithm: str = "HS256"
    jwt_issuer: str | None = None
    jwt_audience: str | None = None

    webhook_secret: str | None = None
    webhook_signature_header: str = "X-Madad-Signature"
    # Subset of backend event types this deployment accepts on the webhook
    # chokepoint. Empty (the default) means "use the dispatcher's bundled
    # ALL_BACKEND_EVENTS set" — operators override per-environment when a
    # specific environment (e.g. staging) should reject Phase 1.b events
    # until those workflows ship.
    webhook_allowed_event_types: set[str] = Field(default_factory=set)


class ObservabilitySettings(BaseModel):
    """Metrics + error tracking.

    Prometheus metrics are exposed at ``/metrics`` (scraped, not authed). Sentry
    is initialised only when ``sentry_dsn`` is set, so dev/tests run with neither
    a Sentry account nor the SDK imported.
    """

    metrics_enabled: bool = True
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.0


class DomainSettings(BaseModel):
    """Public domain structure (``*.ai.madadfintech.com``).

    The agentic platform is hosted under the ``ai`` sub-domain of madadfintech.com,
    separate from Madad's core systems.
    """

    base: str = "ai.madadfintech.com"
    api: str = "api.ai.madadfintech.com"  # conversational/workflow + communication
    ops: str = "ops.ai.madadfintech.com"  # operational visibility (admin)
    cms: str = "cms.ai.madadfintech.com"  # CMS & configuration (admin)
    mcp: str = "mcp.ai.madadfintech.com"  # MCP gateway (restricted)

    @property
    def admin_cors_origins(self) -> list[str]:
        """Allowed browser origins for the admin panels (ops + cms)."""

        return [f"https://{self.ops}", f"https://{self.cms}"]


class WorkflowSettings(BaseModel):
    """Tunables for the shared workflow runtime."""

    # Which adapters to wire. "memory" needs no external services (tests/dev).
    # "redis" / "postgres" enable the production adapters.
    session_backend: str = "memory"  # memory | redis
    event_backend: str = "memory"  # memory | redis
    checkpoint_backend: str = "memory"  # memory | postgres

    # Single-step execution budget (seconds) before a step is timed out.
    # Raised 60→120 (UAT 2026-06-14): the doc classify-and-upload can take >25s
    # per file when the classifier is slow; the per-call wait_for is now 50s, so
    # the node (zip pass + per-file fallback) needs headroom under this budget —
    # otherwise the backend finishes the upload but the agent times out at the
    # old 60s step cap and wrongly reports the docs as still missing.
    step_timeout_seconds: float = 120.0
    # How long a session may wait for inbound input before it is considered
    # lapsed (drives nudge/timeout sweeps). 0 disables.
    session_ttl_seconds: int = 60 * 60 * 24 * 14  # 14 days

    # Default retry policy applied to a workflow step on transient failure.
    retry_max_attempts: int = 3
    retry_base_delay_seconds: float = 0.5
    retry_max_delay_seconds: float = 30.0
    retry_jitter: bool = True

    # Recovery sweep: re-drive runs left mid-step by a crash.
    recovery_batch_size: int = 100


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    app_name: str = "madad_fintech_ai"
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    # Emit logs as JSON (production) or human-friendly console (local dev).
    log_json: bool = False

    # Bearer token gating the admin services (CMS, Operational Visibility).
    # Unset = open (local dev only); set in staging/prod. JWT/OIDC can replace it.
    admin_api_token: str | None = None

    # Inbound/outbound request-correlation header. Echoed on every response and
    # bound to the structured-logging context for end-to-end tracing.
    request_id_header: str = "X-Request-ID"

    domains: DomainSettings = Field(default_factory=DomainSettings)
    persistence: PersistenceSettings = Field(default_factory=PersistenceSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    workflow: WorkflowSettings = Field(default_factory=WorkflowSettings)
    celery: CelerySettings = Field(default_factory=CelerySettings)
    events: EventBusSettings = Field(default_factory=EventBusSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    mcp: McpSettings = Field(default_factory=McpSettings)


settings = Settings()
