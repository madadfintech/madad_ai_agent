"""Shared workflow utilities: ids, time, deterministic backoff, serialization."""

from __future__ import annotations

import hashlib
import random
import time
import uuid
from datetime import UTC, datetime

from .enums import Channel


def utcnow() -> datetime:
    """Timezone-aware current UTC time."""

    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    """Generate a time-sortable, prefixed unique id.

    Format: ``<prefix>_<48-bit ms timestamp hex><random hex>`` — lexically
    sortable by creation time, which keeps run/event listings naturally ordered.
    """

    ts = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    return f"{prefix}_{ts:012x}{uuid.uuid4().hex[:12]}"


def _hash_identity(identity: str) -> str:
    """Stable, non-reversible digest of a channel identity (phone/email).

    Keeps PII (raw phone numbers / email addresses) out of keys and ids while
    remaining deterministic for the same identity.
    """

    return hashlib.sha256(identity.strip().lower().encode("utf-8")).hexdigest()


def derive_session_id(channel: Channel, identity: str) -> str:
    """Deterministic session id from the channel-identity pair.

    The same WhatsApp number / email always maps to the same session id, which
    is what makes reconnect-recovery work without any OTP or login.
    """

    return f"sess_{channel.value}_{_hash_identity(identity)[:24]}"


def derive_thread_id(workflow: str, version: int, session_id: str, run_id: str) -> str:
    """Deterministic LangGraph ``thread_id`` for a run.

    Includes the run id so a fresh start gets a clean checkpoint thread, while
    remaining recomputable for crash recovery (the run record stores it too).
    """

    return f"{workflow}.v{version}:{session_id}:{run_id}"


def compute_backoff(
    attempt: int,
    *,
    base_delay: float,
    max_delay: float,
    jitter: bool = True,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff with optional full jitter.

    ``attempt`` is 1-based (first retry == 1). Returns seconds to sleep before
    the next attempt, capped at ``max_delay``.
    """

    raw = base_delay * float(2 ** max(0, attempt - 1))
    capped = min(raw, max_delay)
    if not jitter:
        return capped
    r = rng or random
    return float(r.uniform(0.0, capped))
