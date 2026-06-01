"""Declarative base + shared column helpers for the JSON-document store pattern."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON
from sqlalchemy.orm import DeclarativeBase

from app.shared.workflow.utils import utcnow

# Portable JSON column type: JSONB on PostgreSQL, JSON1/text on SQLite.
JsonDoc = JSON


class Base(DeclarativeBase):
    """Base for all ORM tables. ``type_annotation_map`` keeps ``dict`` -> JSON."""

    type_annotation_map = {dict[str, Any]: JSON}


def utcnow_iso() -> str:
    """UTC timestamp as an ISO-8601 string (sorts lexicographically for indexes)."""

    return utcnow().isoformat()
