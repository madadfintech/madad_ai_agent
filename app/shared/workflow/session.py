"""Session management — channel-identity sessions with reconnect support.

A session is keyed by the channel-identity pair (WhatsApp E.164 number or email
address). There is no OTP and no login: the same number/email always resolves to
the same session, which is what lets a user disconnect and resume later simply by
sending another message (reconnect recovery).

A session points at its currently-active run; the executor uses that pointer to
resume the right workflow when an inbound message arrives.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from .context import Clock, SystemClock
from .enums import Channel, SessionStatus
from .persistence import WorkflowRun
from .utils import derive_session_id, utcnow


class Session(BaseModel):
    """A conversational session bound to one channel-identity."""

    session_id: str
    channel: Channel
    identity: str
    status: SessionStatus = SessionStatus.ACTIVE

    active_run_id: str | None = None
    active_workflow: str | None = None
    active_version: int | None = None
    thread_id: str | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_activity_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None


class SessionStore(ABC):
    """Port for persisting sessions."""

    @abstractmethod
    async def get(self, session_id: str) -> Session | None: ...

    @abstractmethod
    async def save(self, session: Session) -> Session: ...

    @abstractmethod
    async def delete(self, session_id: str) -> None: ...


class InMemorySessionStore(SessionStore):
    """In-memory session store for tests and dev."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    async def get(self, session_id: str) -> Session | None:
        async with self._lock:
            stored = self._sessions.get(session_id)
            return stored.model_copy(deep=True) if stored else None

    async def save(self, session: Session) -> Session:
        session.updated_at = utcnow()
        async with self._lock:
            self._sessions[session.session_id] = session.model_copy(deep=True)
        return session

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)


class SessionManager:
    """Resolves, binds, and lifecycle-manages sessions."""

    def __init__(
        self,
        store: SessionStore,
        *,
        clock: Clock | None = None,
        ttl_seconds: int = 60 * 60 * 24 * 14,
    ) -> None:
        self._store = store
        self._clock = clock or SystemClock()
        self._ttl = ttl_seconds

    async def resolve(self, channel: Channel, identity: str) -> Session:
        """Get the session for a channel-identity, creating it if absent."""

        session_id = derive_session_id(channel, identity)
        session = await self._store.get(session_id)
        if session is None:
            session = Session(
                session_id=session_id,
                channel=channel,
                identity=identity,
                status=SessionStatus.ACTIVE,
            )
            await self._store.save(session)
        return session

    async def get(self, channel: Channel, identity: str) -> Session | None:
        return await self._store.get(derive_session_id(channel, identity))

    def _expiry(self) -> datetime | None:
        if self._ttl <= 0:
            return None
        return self._clock.now() + timedelta(seconds=self._ttl)

    async def bind_run(self, session: Session, run: WorkflowRun) -> Session:
        session.active_run_id = run.run_id
        session.active_workflow = run.workflow
        session.active_version = run.version
        session.thread_id = run.thread_id
        session.status = SessionStatus.ACTIVE
        session.last_activity_at = self._clock.now()
        return await self._store.save(session)

    async def mark_waiting(self, session: Session) -> Session:
        session.status = SessionStatus.WAITING
        session.last_activity_at = self._clock.now()
        session.expires_at = self._expiry()
        return await self._store.save(session)

    async def mark_active(self, session: Session) -> Session:
        session.status = SessionStatus.ACTIVE
        session.last_activity_at = self._clock.now()
        return await self._store.save(session)

    async def complete(self, session: Session) -> Session:
        session.status = SessionStatus.COMPLETED
        session.last_activity_at = self._clock.now()
        session.expires_at = None
        return await self._store.save(session)

    async def expire(self, session: Session) -> Session:
        session.status = SessionStatus.EXPIRED
        return await self._store.save(session)

    async def forget(self, channel: Channel, identity: str) -> bool:
        """Delete the session record for a channel-identity pair.

        Returns True when a session existed and was removed, False when
        nothing was stored — used by the admin reset endpoint so the SME
        starts the next inbound from a clean slate.
        """

        session_id = derive_session_id(channel, identity)
        existed = await self._store.get(session_id) is not None
        await self._store.delete(session_id)
        return existed
