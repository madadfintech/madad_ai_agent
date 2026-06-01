# MADAD AI Agent

> Conversational onboarding platform for SME invoice financing in Qatar.

The MADAD AI Agent is the agentic layer of the MADAD FinTech platform. It drives
the end-to-end SME onboarding journey over WhatsApp and email, calling the
MADAD backend exclusively through the MCP cluster. It owns conversation,
orchestration, session continuity, notifications, and operational visibility;
it does **not** own SME business data — Madad's backend remains the system of
record, and the platform deliberately keeps no document bytes or extracted
fields.

## Status

- Phase 1.a (Steps 1–8 onboarding) **code-complete except MCP catalog integration**.
- **210 tests + 1 CI-only skip** green; `ruff` + `mypy --strict` (on `app/`) clean.
- All six services, async workers, persistence, observability, security, Docker
  stack and CI are shipped. MCP scaffold in place; final integration against the
  real MCP cluster is the next phase of work.

## Overview

The platform is implemented as a **FastAPI service fleet** orchestrated by a
shared **LangGraph v1.0** conversational runtime. Each service is a separate
deployable container; the runtime drives the SME through the onboarding flow
across channels, services, and external integrations using a deterministic
graph with pause/resume on interrupt for inbound replies or external
decisions.

Architectural principles:

- **Channel = identity.** A verified WhatsApp number or email address is the
  session key. The Madad backend's channel-session bridge issues scoped tokens
  after verifying the channel; no separate OTP is performed inside the agent
  conversation.
- **Single integration surface.** The agent calls Madad backend systems
  exclusively through the MCP cluster (the [`madad_ai_mcp_cluster`](https://github.com/madadfintech/madad_ai_mcp_cluster)).
  There is no direct REST integration with Madad APIs from this codebase.
- **Data sovereignty.** SME documents and extracted fields live in Madad's
  vault; this platform keeps only orchestration metadata, conversation history,
  and operational visibility. No object storage for SME bytes.
- **Ports and adapters everywhere.** Every external dependency is behind an
  abstract port with an in-memory fake (used in tests and dev) and a real
  adapter (lazy-imported). Backends are selected from settings.

## Architecture

Six FastAPI services, three async worker processes, and a shared workflow
runtime.

| Service | Port | Role |
|---|---|---|
| **Conversational Workflow** | 8001 | Runs the onboarding `LangGraph` graph; entry for inbound messages and webhooks. Owns the Phase 1.a Steps 1–8 flow. |
| **Communication** | 8002 | All inbound / outbound messaging. Renders CMS templates; dispatches via MCP. WhatsApp + email. |
| **CMS & Configuration** | 8003 | Versioned runtime configuration: templates, checklists, nudge schedules, settings. Channel + locale aware. |
| **Nudge & Notification** | 8004 | Reminder sequence orchestration. Lazy per-step scheduling, retry, suppression, escalation. |
| **Document Intelligence** | 8005 | Document orchestration — routes documents to Madad's vault via MCP; checklist tracking; no bytes stored locally. |
| **Operational Visibility** | 8006 | Cross-service activity log, conversation replay, funnel/metric projections. |

Async workers:

- **Celery worker** + **Celery beat** — periodic jobs (nudge run-due drain, workflow recovery, timeout sweeps).
- **Visibility stream consumer** — drains the unified cross-process event stream into the visibility activity store.

Cross-cutting:

- **Shared workflow runtime** (`app/shared/workflow/`) — LangGraph v1.0 with the
  production concerns layered on: retry, timeout, audit, events, persistence,
  session resolution, recovery, snapshot/resume.
- **Unified event bus** (`app/shared/events/`) — single cross-process `Event`
  envelope. In-process default; Redis Streams transport in production.
- **MCP integration scaffold** (`app/shared/mcp/`) — `MCPToolCaller` protocol,
  client base with timeout/retry/error-mapping, in-memory recording fake, real
  client skeleton (will be backed by the official `fastmcp` SDK on integration).
- **Shared HTTP foundation** (`app/core/`) — `create_service_app` factory:
  correlation-ID middleware, uniform `AppError → HTTP` mapping, `/health` and
  `/ready` probes (with backend pings), CORS, lifespans.

## Tech stack

| Concern | Choice |
|---|---|
| HTTP framework | FastAPI 0.115, uvicorn |
| Workflow engine | LangGraph v1.2 (no LangChain agents — flows are deterministic) |
| Async I/O | `asyncio`, `httpx` |
| Persistence | PostgreSQL 16, SQLAlchemy 2.0 async (`asyncpg`), Alembic |
| Cache + sessions + streams | Redis 7 (`redis.asyncio`) |
| Async scheduling | Celery 5.6 (worker + beat) |
| Configuration | Pydantic Settings (nested env vars via `__`) |
| Logging | `structlog` (JSON in prod, console in dev), correlation-ID middleware |
| Metrics | Prometheus (`prometheus-client`) |
| Error tracking | Sentry (DSN-gated, lazy) |
| Security | PyJWT, HMAC webhook verification, admin bearer token |
| Containerisation | Docker (`python:3.11-slim`), Docker Compose v2 |
| CI | GitHub Actions (ruff + mypy + pytest; real Postgres + Redis integration job) |

## Repository layout

```
app/
  core/                      # config, logging, exceptions, app factory, middleware,
                             #   security seams, observability, readiness
  shared/                    # cross-service shared infrastructure
    workflow/                # LangGraph runtime (executor, recovery, sessions, events…)
    events/                  # unified cross-process event bus (in-process + Redis Streams)
    db/                      # async SQLAlchemy engine, schema translation, migrations base
    mcp/                     # MCP integration scaffold (protocol, client, registry)
    i18n.py                  # Locale enum
  services/
    workflow/                # Conversational Workflow service (port 8001) + onboarding graph
    communication/           # Communication service (port 8002)
    cms/                     # CMS & Configuration (port 8003)
    nudge/                   # Nudge & Notification (port 8004)
    document/                # Document Intelligence (port 8005)
    visibility/              # Operational Visibility (port 8006)
  workers/                   # Celery app + tasks + jobs
  main.py                    # root health app
tests/                       # 210+ tests across services + integration
docker/
  Dockerfile                 # single image — all services run from it
  docker-compose.yml         # full local/staging stack (14 containers)
  nginx.conf                 # edge reverse proxy
  prometheus.yml             # metrics scrape config
migrations/                  # Alembic
.github/workflows/ci.yml     # CI pipeline
.env.example                 # configuration reference (every nested setting)
requirements.txt             # runtime + test dependencies
```

## Getting started

### Prerequisites

- **Python 3.11** for the production toolchain (the pinned deps have 3.11
  wheels). Local development can use 3.12+, but stay on 3.11 for the closest
  parity with the Docker image.
- **Docker Engine ≥ 24** with the Compose v2 plugin (`docker compose ...`).
- **`gh` CLI** if you'll interact with GitHub from the command line.

### Local development (venv)

```bash
python -m venv .venv
. .venv/Scripts/activate            # on Windows; use `source .venv/bin/activate` elsewhere
pip install -r requirements.txt
cp .env.example .env
```

Run a single service for development:

```bash
uvicorn app.services.workflow.main:app --reload --port 8001
```

Or boot the root app for a quick health check:

```bash
uvicorn app.main:app --reload
# http://localhost:8000/health
```

### Full stack via Docker

The compose stack runs Postgres, Redis, a one-shot Alembic migrate job, the six
services, Celery worker + beat, the visibility consumer, NGINX, Prometheus and
Grafana.

```bash
cp .env.example .env             # adjust secrets if needed
docker compose -f docker/docker-compose.yml up --build
```

After everything reports healthy:

- API edge: `http://localhost/` (NGINX, host-routed in production)
- Service health probes: `http://localhost:8001/health` … `8006/health`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000` (default `admin` / `${GRAFANA_PASSWORD}`)

Apply migrations stand-alone (the compose `migrate` service does this
automatically):

```bash
alembic upgrade head
```

### Testing and gates

Three gates must stay green at every commit:

```bash
ruff check app tests
mypy app                          # strict on app/; tests are intentionally un-annotated
pytest -q                         # 210 pass + 1 skipped (the Redis-streams integration runs in CI)
```

CI also runs an **integration job** with real Postgres and Redis containers,
which exercises the persistence adapters and the unified event bus against
production-equivalent backends.

## Configuration

All configuration is environment-driven via Pydantic Settings. Nested settings
use a double-underscore delimiter (`POSTGRES__DSN` → `settings.postgres.dsn`).
**Unset auth secrets keep the corresponding gate dev-open** — set them in
staging and production.

Highlights (see `.env.example` for the full list):

| Variable | Purpose |
|---|---|
| `PERSISTENCE__BACKEND` | `memory` (default) / `postgres` |
| `POSTGRES__DSN` | `postgresql+asyncpg://user:pw@host:5432/db` (alembic swaps to `+psycopg` for migrations) |
| `REDIS__URL` | Redis URL for sessions, cache, streams |
| `EVENTS__TRANSPORT` | `memory` (default) / `redis` (Redis Streams in production) |
| `CELERY__BROKER_URL`, `CELERY__RESULT_BACKEND` | Redis logical DBs 1 and 2 |
| `MCP__ENABLED` | `false` (default — uses in-memory MCP fakes) / `true` (calls the real cluster) |
| `MCP__ENDPOINT` | MCP cluster base URL |
| `SECURITY__JWT_SECRET` | Public-API bearer JWT — required in prod |
| `SECURITY__WEBHOOK_SECRET` | HMAC over Madad-platform callback bodies — required in prod |
| `ADMIN_API_TOKEN` | Admin bearer (CMS + Operational Visibility panels) |
| `OBSERVABILITY__SENTRY_DSN` | Sentry DSN; empty disables Sentry |

## MCP integration

The platform interacts with Madad backend systems **only** through the MCP
cluster: [`madadfintech/madad_ai_mcp_cluster`](https://github.com/madadfintech/madad_ai_mcp_cluster).

- **Deployed at:** `https://madad-mcp-cluster-626656664233.me-central1.run.app/mcp`
- **Transport:** official MCP JSON-RPC over Streamable HTTP (Anthropic `fastmcp` SDK)
- **Catalog:** 59 tools across six groups (auth, external communications, MCP
  agent orchestration, KYC, offers, monetization payments)
- **Auth model:** transport is restricted in production via Cloud Run IAM;
  per-tool auth uses a scoped `access_token` issued by the channel-session
  bridge (`madad_mcp_create_channel_session`).

The agentic platform's MCP integration scaffold lives in `app/shared/mcp/` and
the workflow-side adapters in `app/services/workflow/mcp_adapters.py`. The
real integration is staged in five phases under the `mcp.enabled` feature flag:

1. Foundation swap to `fastmcp.Client` with the 59-tool registry.
2. Channel-session bridge + auth adapter.
3. Onboarding graph reshape against the Final Agent Flow Contract.
4. Monetization payment block (QAR 6,000 onboarding fee).
5. Webhook receivers + on-demand status polling worker.

## CI

`.github/workflows/ci.yml` runs:

- **quality** — `ruff check`, `mypy app`, `pytest -q`
- **integration** — same plus a real Postgres + Redis service-container pair,
  exercising the persistence + event-bus adapters end-to-end

Both jobs run on every push to `main` and every pull request.

## Deployment

The compose stack is the local + staging deployment unit. Production runs the
same image under lightweight Kubernetes (K3s) or any container scheduler that
honours the service health probes.

Reference sizing:

| Environment | vCPU | RAM | Disk | OS |
|---|---:|---:|---:|---|
| Staging (Phase 1.a) | 4 | 8 GB | 50 GB SSD | Ubuntu 24.04 LTS |
| Production target | 4 | 16 GB | 200 GB SSD | Ubuntu 24.04 LTS |

The MCP cluster runs on Google Cloud Run as a separate service operated by the
MCP cluster team; the agentic platform consumes it but does not host it.

## Conventions

- **Commits** — Conventional Commits style (`feat:`, `fix:`, `refactor:`, `perf:`, `chore:`, `ci:`, `docs:`).
- **Branches** — Feature branches off `main`, fast-forward merge.
- **Tests** — Strict mypy on `app/` only; tests are un-annotated by convention.
- **Logs** — Structured JSON (`LOG_JSON=true` in prod); every request carries a
  correlation ID via the `X-Request-ID` header.
- **Errors** — All deliberate errors extend `AppError` with an `http_status`;
  one handler in `create_service_app` maps them uniformly across every service.

## License

Proprietary — Madad FinTech. All rights reserved.

## Contacts

- Agentic platform: Jathish Namboothiri (Lead Platform Engineer)
- MCP cluster: Ishan (separate repository)
- Madad platform: Madad FinTech engineering team
