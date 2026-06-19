"""SQLite persistence for the monitor UI.

Three tables:
* ``events``      — every issue ever captured (survives monitor /clear)
* ``identities``  — saved test-user identities for one-click wipe
* ``cleanups``    — audit trail of every cleanup_test_users run from the UI
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine, select

from .config import SQLITE_PATH


class Event(SQLModel, table=True):
    """One captured issue. Mirrors the log monitor's issues.log entries +
    keeps them around after a /monitor/clear so historical context isn't
    lost. The ``key`` field is a stable fingerprint (rule + container +
    line) used to dedupe re-imports during polling."""

    id: Optional[int] = Field(default=None, primary_key=True)
    at: str  # ISO timestamp from the monitor
    container: str
    rule: str
    severity: str
    description: str = ""
    line: str
    seen_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    key: str = Field(index=True)  # for dedupe


class Identity(SQLModel, table=True):
    """A saved test-user identity for quick reuse."""

    id: Optional[int] = Field(default=None, primary_key=True)
    identity: str = Field(unique=True, index=True)
    label: str = ""
    added_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class Cleanup(SQLModel, table=True):
    """One row per cleanup_test_users invocation from the UI."""

    id: Optional[int] = Field(default=None, primary_key=True)
    at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    identities: str  # JSON-encoded list
    pattern: str = ""
    dry_run: bool = False
    summary: str = ""  # JSON-encoded {table: count}
    success: bool = True
    error: str = ""


engine = create_engine(
    f"sqlite:///{SQLITE_PATH}",
    echo=False,
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    return Session(engine)


def upsert_event(session: Session, event: Event) -> None:
    """Insert an event if its key isn't already in the DB."""
    exists = session.exec(
        select(Event.id).where(Event.key == event.key)
    ).first()
    if exists is not None:
        return
    session.add(event)
    session.commit()


def event_key(at: str, container: str, rule: str, line: str) -> str:
    """Stable identity for an event so polling doesn't double-insert."""
    return f"{at}|{container}|{rule}|{hash(line) & 0xffffffff:08x}"
