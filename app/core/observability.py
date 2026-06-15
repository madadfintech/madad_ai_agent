"""Metrics (Prometheus) + error tracking (Sentry).

Prometheus collectors are module-level singletons registered in the default
registry; :class:`PrometheusMiddleware` records one counter + one latency
observation per request, and :func:`metrics_endpoint` serves the registry at
``/metrics``. Labels are kept low-cardinality (service, method, status) — no raw
paths. Sentry is initialised lazily and only when a DSN is configured.
"""

from __future__ import annotations

import time

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import Settings
from app.core.logging import get_logger

_REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests.",
    ["service", "method", "path", "status"],
)
_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["service", "method", "path"],
)


def _path_label(request: Request) -> str:
    """Stable, low-cardinality path label for Prometheus.

    Prefer the matched route's templated path (``/runs/{run_id}``) so
    every concrete ID collapses to the same series instead of exploding
    cardinality. Falls back to the raw URL path only when no route matched
    (probes, 404s, middleware-early responses).
    """
    route = request.scope.get("route") if request.scope else None
    template = getattr(route, "path", None) if route is not None else None
    if isinstance(template, str) and template:
        return template
    return request.url.path or "unknown"


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Record request count + latency for every request."""

    def __init__(self, app: object, *, service: str) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._service = service

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        start = time.perf_counter()
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            path = _path_label(request)
            _REQUESTS.labels(self._service, request.method, path, "500").inc()
            _LATENCY.labels(self._service, request.method, path).observe(
                time.perf_counter() - start
            )
            raise
        path = _path_label(request)
        _REQUESTS.labels(self._service, request.method, path, str(status)).inc()
        _LATENCY.labels(self._service, request.method, path).observe(time.perf_counter() - start)
        return response


async def metrics_endpoint(_request: Request) -> Response:
    """Expose the Prometheus registry (scraped; not behind admin auth)."""

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


def init_sentry(settings: Settings, service: str) -> bool:
    """Initialise Sentry if a DSN is configured. Returns whether it was enabled.

    The SDK is imported lazily so neither dev nor tests need it installed.
    """

    dsn = settings.observability.sentry_dsn
    if not dsn:
        return False
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=settings.app_env,
        traces_sample_rate=settings.observability.sentry_traces_sample_rate,
    )
    sentry_sdk.set_tag("service", service)
    get_logger("observability").info("sentry.initialised", service=service)
    return True
