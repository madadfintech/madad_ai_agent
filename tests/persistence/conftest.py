"""Shared DB fixture for Postgres-adapter tests.

By default this runs against SQLite (aiosqlite) on a temp-file database so the
same ORM models + store adapters used in production get real SQL round-trip
coverage without a running Postgres. Schema tokens are translated to the default
schema by ``Database``.

Set ``TEST_DATABASE_URL`` (e.g. ``postgresql+asyncpg://user:pw@host/db``) to run
the identical tests against a real PostgreSQL — this is what the CI integration
job does, exercising the actual per-domain schemas. Tables are dropped on
teardown so each test is isolated on the shared database.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest_asyncio

# Import all table modules so Base.metadata is fully populated for create_all.
import app.shared.db.metadata  # noqa: F401
from app.shared.db import Database

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@pytest_asyncio.fixture
async def db(tmp_path) -> AsyncIterator[Database]:
    url = TEST_DATABASE_URL or f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db"
    database = Database(url)
    await database.create_all()
    try:
        yield database
    finally:
        await database.drop_all()  # isolate tests on a shared (Postgres) DB
        await database.dispose()
