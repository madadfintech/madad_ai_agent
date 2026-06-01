"""Shared async persistence foundation (SQLAlchemy 2.0).

Production runs PostgreSQL (asyncpg) with one logical schema per domain; tests run
SQLite (aiosqlite) with the schemas translated to the default schema. The same
ORM models and store adapters work against both via a schema-translate map.

Stores follow a JSON-document pattern: the authoritative pydantic domain model is
serialized into a ``data`` JSON column, alongside a few indexed columns used for
queries. This keeps the domain models the single source of truth and the schema
migration-light.
"""

from __future__ import annotations

from .base import Base, JsonDoc, utcnow_iso
from .engine import SCHEMAS, Database

__all__ = ["Base", "JsonDoc", "utcnow_iso", "Database", "SCHEMAS"]
