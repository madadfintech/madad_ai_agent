"""Async engine/session management with PostgreSQL-schema translation.

``Database`` wraps an async engine + session factory. On PostgreSQL it uses the
real per-domain schemas; on SQLite (tests) it translates every schema token to
the default schema so the same models/queries run unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .base import Base

# Logical schemas (PostgreSQL). Translated to the default schema on SQLite.
SCHEMAS: tuple[str, ...] = (
    "workflow",
    "communication",
    "cms",
    "nudge",
    "document",
    "audit",
)


class Database:
    """Owns the async engine + session factory for a service/process."""

    def __init__(
        self,
        dsn: str,
        *,
        echo: bool = False,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_recycle_seconds: int = 1800,
    ) -> None:
        is_sqlite = dsn.startswith("sqlite")
        kwargs: dict[str, object] = {"echo": echo, "pool_pre_ping": True}
        # Connection-pool sizing applies to real servers (Postgres); SQLite's
        # default pool ignores these and rejects some of them.
        if not is_sqlite:
            kwargs["pool_size"] = pool_size
            kwargs["max_overflow"] = max_overflow
            kwargs["pool_recycle"] = pool_recycle_seconds
        engine = create_async_engine(dsn, **kwargs)
        # SQLite has no schemas: map every schema token to the default schema.
        if is_sqlite:
            engine = engine.execution_options(
                schema_translate_map={schema: None for schema in SCHEMAS}
            )
        self._engine = engine
        self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    @property
    def is_sqlite(self) -> bool:
        return self._engine.dialect.name == "sqlite"

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session scope that commits on success and rolls back on error."""

        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def create_all(self) -> None:
        """Create schemas (Postgres) + all tables. For tests/dev; prod uses Alembic."""

        async with self._engine.begin() as conn:
            if not self.is_sqlite:
                from sqlalchemy.schema import CreateSchema

                for schema in SCHEMAS:
                    await conn.execute(CreateSchema(schema, if_not_exists=True))
            await conn.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        """Drop all tables (test isolation on a shared DB). For tests only."""

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def ping(self) -> None:
        """Execute ``SELECT 1`` to verify connectivity (readiness probes)."""

        from sqlalchemy import text

        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self._engine.dispose()
