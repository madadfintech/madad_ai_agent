"""Readiness checks for the ``/ready`` probe.

A readiness check is a named async callable that raises on failure. The shared
app factory runs the registered checks; all-pass = 200 ``ready``, any-fail = 503
``not_ready`` with the failing check named. Liveness (``/health``) stays trivial —
it answers "the process is up", readiness answers "its dependencies are usable".

``default_checks`` derives the right set from settings, so a memory-backed
service (tests/dev) has zero checks and is always ready, while a Postgres/Redis
deployment pings its backends.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from app.core.config import Settings

# (name, check) — the check raises if the dependency is unusable.
ReadinessCheck = tuple[str, Callable[[], Awaitable[None]]]


def postgres_check() -> ReadinessCheck:
    async def check() -> None:
        from app.shared.db.provider import get_database

        await get_database().ping()

    return ("postgres", check)


def redis_check(url: str) -> ReadinessCheck:
    async def check() -> None:
        import redis.asyncio as aioredis

        client = aioredis.from_url(url)
        try:
            await cast("Awaitable[object]", client.ping())
        finally:
            await client.aclose()

    return ("redis", check)


def default_checks(settings: Settings) -> list[ReadinessCheck]:
    """Pick readiness checks for the backends this deployment actually uses."""

    checks: list[ReadinessCheck] = []
    if settings.persistence.backend == "postgres":
        checks.append(postgres_check())
    uses_redis = (
        settings.persistence.cms_cache == "redis"
        or settings.workflow.session_backend == "redis"
        or settings.workflow.event_backend == "redis"
        or settings.events.transport == "redis"
    )
    if uses_redis:
        checks.append(redis_check(settings.redis.url))
    return checks
