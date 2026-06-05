"""Postgres checkpointer adapter (production durable checkpointing).

Wraps LangGraph's ``AsyncPostgresSaver`` behind the :class:`CheckpointerProvider`
port. The saver is created from the configured DSN and kept open for the process
lifetime; ``setup`` creates the checkpoint tables if missing.

``langgraph-checkpoint-postgres`` is imported lazily so this module is importable
without it; it is only constructed when ``checkpoint_backend = "postgres"``.
"""

from __future__ import annotations

from typing import Any

from app.core.config import PostgresSettings

from ..checkpoint import CheckpointerProvider
from ..errors import CheckpointError


class PostgresCheckpointerProvider(CheckpointerProvider):
    """Durable LangGraph checkpointer backed by PostgreSQL."""

    def __init__(self, settings: PostgresSettings) -> None:
        self._settings = settings
        self._cm: Any | None = None
        self._saver: Any | None = None

    async def setup(self) -> None:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise CheckpointError(
                "langgraph-checkpoint-postgres is not installed",
            ) from exc

        # ``from_conn_string`` yields an async context manager; enter it manually
        # so the saver lives for the whole process and close it in ``aclose``.
        # The langgraph checkpointer uses psycopg directly, so feed it the plain
        # libpq DSN (no SQLAlchemy +asyncpg dialect prefix).
        self._cm = AsyncPostgresSaver.from_conn_string(self._settings.libpq_dsn)
        self._saver = await self._cm.__aenter__()
        await self._saver.setup()

    def get(self) -> Any:
        if self._saver is None:  # pragma: no cover - misuse guard
            raise CheckpointError("PostgresCheckpointerProvider.setup() not called")
        return self._saver

    async def aclose(self) -> None:
        if self._cm is not None:
            await self._cm.__aexit__(None, None, None)
            self._cm = None
            self._saver = None
