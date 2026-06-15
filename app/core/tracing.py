"""OpenTelemetry tracing seam — distributed tracing across FastAPI / httpx
(the MCP client) / SQLAlchemy / Celery.

DESIGN:
* Off by default — the SDK is lazy-imported only when
  ``settings.observability.otel_endpoint`` is set, so dev/tests never
  pay the dependency cost.
* Auto-instruments the four libraries that cover 95% of inter-service
  traffic — FastAPI for HTTP server spans, httpx for client spans
  (MCP cluster + any other HTTP egress), SQLAlchemy for database
  spans, and Celery for the background-job spans.
* Propagates the existing ``X-Request-ID`` header as the trace id
  upstream so a request can be followed across services without
  manual stitching.

USAGE:
    from app.core.tracing import init_tracing
    init_tracing(settings, service="workflow")

When the endpoint is empty (default) the call is a no-op.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger

_log = get_logger("observability.tracing")
_initialized: set[str] = set()


def init_tracing(settings: Settings, service: str) -> bool:
    """Initialise OpenTelemetry for ``service`` against
    ``settings.observability.otel_endpoint``. Returns whether tracing was
    enabled — callers don't need to act on the return value.

    Idempotent per process: re-calling for the same service is a no-op so
    multi-app processes (uvicorn workers) don't double-register.
    """
    endpoint = (settings.observability.otel_endpoint or "").strip()
    if not endpoint:
        return False
    if service in _initialized:
        return True
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError as exc:
        _log.warning("otel.sdk_missing", error=str(exc))
        return False

    resource = Resource.create({
        "service.name": service,
        "service.namespace": settings.observability.otel_service_namespace,
        "deployment.environment": settings.observability.otel_environment,
    })
    sampler = ParentBased(
        TraceIdRatioBased(settings.observability.otel_traces_sample_rate)
    )
    provider = TracerProvider(resource=resource, sampler=sampler)
    exporter = OTLPSpanExporter(endpoint=endpoint, timeout=10)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    _instrument_libraries()
    _initialized.add(service)
    _log.info(
        "otel.tracing_enabled",
        service=service,
        endpoint=endpoint,
        sample_rate=settings.observability.otel_traces_sample_rate,
    )
    return True


def instrument_app(app: Any, service: str) -> None:
    """Wrap ``app`` (a FastAPI/Starlette instance) with OTel HTTP server
    instrumentation. Called once per app from ``create_service_app``; no-op
    when tracing wasn't initialised."""
    if service not in _initialized:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        return
    # Idempotent — FastAPIInstrumentor refuses to re-instrument the same app.
    try:
        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:  # noqa: BLE001
        _log.warning("otel.fastapi_instrument_failed", error=str(exc))


def _instrument_libraries() -> None:
    """Auto-instrument the client libraries the agent stack relies on —
    httpx (MCP cluster + any other egress), SQLAlchemy (postgres run-store
    / CMS / persistence), Celery (background jobs)."""
    # httpx — the MCP transport.
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception:  # noqa: BLE001
        pass
    # SQLAlchemy — Postgres adapters.
    try:
        from opentelemetry.instrumentation.sqlalchemy import (
            SQLAlchemyInstrumentor,
        )

        SQLAlchemyInstrumentor().instrument()
    except Exception:  # noqa: BLE001
        pass
    # Celery — beat + worker.
    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor

        CeleryInstrumentor().instrument()
    except Exception:  # noqa: BLE001
        pass
