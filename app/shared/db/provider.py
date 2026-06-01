"""Process-singleton :class:`Database` built from settings.

Service factories call :func:`get_database` when ``persistence.backend=postgres``
so every store in a process shares one engine/pool.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import settings

from .engine import Database


@lru_cache(maxsize=1)
def get_database() -> Database:
    pg = settings.postgres
    return Database(
        pg.dsn,
        pool_size=pg.pool_size,
        max_overflow=pg.max_overflow,
        pool_recycle_seconds=pg.pool_recycle_seconds,
    )
