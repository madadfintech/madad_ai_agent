"""Shared FastAPI application factory.

Every service builds its app through :func:`create_service_app` so the
cross-cutting concerns — correlation-id middleware + access logging, a single
``AppError`` -> HTTP handler (status read from the error), liveness (``/health``)
and readiness (``/ready``) probes, and admin CORS/auth — live in one place
instead of being copy-pasted into six ``main.py`` files. Service-specific startup
(forwarder wiring, runtime setup) is supplied via ``lifespan``.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import Settings
from app.core.config import settings as default_settings
from app.core.exceptions import AppError
from app.core.middleware import RequestContextMiddleware
from app.core.readiness import ReadinessCheck, default_checks
from app.core.security import require_admin, require_api_auth

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_service_app(
    *,
    title: str,
    service: str,
    version: str = "0.1.0",
    admin: bool = False,
    api_auth: bool = False,
    lifespan: Lifespan | None = None,
    readiness_checks: list[ReadinessCheck] | None = None,
    metrics: bool | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Build a FastAPI app with the platform's cross-cutting concerns wired in.

    ``admin=True`` adds the bearer-token gate + CORS for the operator panels
    (CMS, Operational Visibility). ``api_auth=True`` gates the public API with a
    service JWT (probes + webhooks exempt). ``readiness_checks`` defaults to the
    backends this deployment uses (see :func:`default_checks`). ``metrics``
    overrides the ``observability.metrics_enabled`` setting.
    """

    settings = settings or default_settings
    checks = readiness_checks if readiness_checks is not None else default_checks(settings)
    enable_metrics = settings.observability.metrics_enabled if metrics is None else metrics

    deps = []
    if admin:
        deps.append(Depends(require_admin))
    if api_auth:
        deps.append(Depends(require_api_auth))

    app = FastAPI(
        title=title,
        version=version,
        lifespan=lifespan,
        dependencies=deps or None,
    )
    app.add_middleware(
        RequestContextMiddleware, service=service, header=settings.request_id_header
    )
    from app.core.observability import (
        PrometheusMiddleware,
        init_sentry,
        metrics_endpoint,
    )
    from app.core.tracing import init_tracing, instrument_app

    init_sentry(settings, service)  # no-op unless a DSN is configured
    # OpenTelemetry — no-op unless settings.observability.otel_endpoint is set.
    init_tracing(settings, service)
    instrument_app(app, service)
    if enable_metrics:
        app.add_middleware(PrometheusMiddleware, service=service)
        app.add_api_route("/metrics", metrics_endpoint, methods=["GET"], tags=["ops"])
    if admin:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.domains.admin_cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(AppError)
    async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": service}

    @app.get("/ready", tags=["ops"])
    async def ready() -> JSONResponse:
        failed: dict[str, str] = {}
        for name, check in checks:
            try:
                await check()
            except Exception as exc:  # noqa: BLE001 - report, don't crash the probe
                failed[name] = str(exc)
        if failed:
            return JSONResponse(
                status_code=503, content={"status": "not_ready", "failed": failed}
            )
        return JSONResponse(
            content={"status": "ready", "checks": [name for name, _ in checks]}
        )

    return app
