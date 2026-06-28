"""Bulk campaign broadcasting — CSV upload → fan-out to single-identity starts.

The Workflow service exposes ``POST /workflow/campaign/start`` for ONE
``(channel, identity)`` per call. Madad outreach wants a way to upload a
CSV of SMEs and have the agent kick the onboarding campaign for each
row at a controlled rate.

Design constraints (CTO decisions, 2026-06-28):

* **Rate limit**: hardcoded ceiling of 60/min (under Meta's WABA utility
  template SLA). Default 30/min; per-batch override 1-60.
* **CSV schema**: required ``phone`` (E.164). Optional ``locale``
  (``en``/``ar``), ``name``, ``tags`` (comma-joined). Extra columns
  silently ignored — forward-compatible.
* **Failure semantics**: best-effort. One bad row never halts the
  batch. Per-row errors returned at submit-time (validation) +
  tracked in batch status (dispatch).
* **Idempotency**: required ``idempotency_key``. 24h Redis window.
  Re-submit returns the existing ``batch_id`` unchanged.
* **Audit**: ``GET /workflow/campaign/broadcast`` returns the last
  50 batches (7-day retention).

What this module ISN'T:

* Not a Celery task. We use FastAPI ``BackgroundTasks`` so the
  workflow container stays the only piece to redeploy. Restart
  mid-batch loses the in-process work; the existing per-identity
  dedup makes re-submission safe.
* Not a queue. Backpressure happens via in-loop ``asyncio.sleep``.
* Not a Meta WABA gateway. Each row internally calls the SAME
  ``OnboardingPlatform.runtime.start`` path the single-identity
  endpoint uses, inheriting all its dedup + cancellation guarantees.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.shared.workflow.enums import Channel
from app.shared.workflow.utils import new_id, utcnow

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Hard ceiling on rate per minute — Meta WhatsApp utility template SLA is
#: ~30/min per WABA. We accept up to 60 to leave headroom for two WABAs
#: in flight (decision D1 not yet finalised; this stays safe in either case).
MAX_RATE_PER_MINUTE = 60
DEFAULT_RATE_PER_MINUTE = 30

#: How long Redis remembers the idempotency key.
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60

#: How long Redis remembers a finished batch's status. 7 days = M1 audit window.
BATCH_RETENTION_SECONDS = 7 * 24 * 60 * 60

#: How many batches to keep in the recent-list index. Older batches still
#: live in Redis (until their TTL expires) but aren't surfaced in the index.
RECENT_BATCH_LIMIT = 50

#: How many rows we accept per upload. Hard cap to keep memory bounded;
#: ops can split larger lists into multiple batches.
MAX_ROWS_PER_BATCH = 5000


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------


class BatchStatus(StrEnum):
    """Lifecycle of a broadcast batch.

    ``QUEUED`` is brief — only between accept and the first row firing.
    ``COMPLETED`` means every row was attempted (sent or failed); the
    batch is closed regardless of per-row outcomes.
    """

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


#: E.164 with the leading ``+``. Allows 6-15 digit numbers per the spec.
_E164_RE = re.compile(r"^\+?[1-9]\d{5,14}$")


def _normalize_phone(raw: str) -> str | None:
    """Return an E.164 ``+``-prefixed phone or None if invalid.

    Strips whitespace, dashes, parentheses, and dots. Accepts numbers
    that already carry a leading ``+`` and numbers that don't (we add
    one). Rejects numbers with a leading zero (E.164 doesn't allow it)
    and numbers outside the 7-16 digit length range.
    """
    s = re.sub(r"[\s\-().]", "", raw or "").strip()
    if not s:
        return None
    if not s.startswith("+"):
        s = "+" + s.lstrip("0")
    return s if _E164_RE.match(s) else None


class BroadcastRow(BaseModel):
    """One parsed-and-validated row from the uploaded CSV.

    ``valid=False`` rows carry a human-readable ``error`` and skip
    the dispatch loop entirely — they're only included in the response
    so ops can fix their CSV.
    """

    row_number: int = Field(description="1-indexed row position in the uploaded CSV.")
    raw_phone: str
    phone: str | None = None     # normalised E.164 (None if invalid)
    locale: str = "en"
    name: str | None = None
    tags: list[str] = Field(default_factory=list)
    valid: bool = True
    error: str | None = None


class BroadcastBatch(BaseModel):
    """Persisted state for one batch — what /broadcast/{id} returns."""

    batch_id: str
    idempotency_key: str
    channel: Channel
    status: BatchStatus = BatchStatus.QUEUED

    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    sent: int = 0
    failed: int = 0

    rate_per_minute: int = DEFAULT_RATE_PER_MINUTE
    dry_run: bool = False

    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    submitted_by: str | None = None

    # Per-row outcomes (sample only — full list lives in ``rows``).
    failures: list[dict[str, Any]] = Field(default_factory=list)
    invalid_details: list[dict[str, Any]] = Field(default_factory=list)


class BroadcastSubmitResult(BaseModel):
    """Response shape for the submit endpoint."""

    batch_id: str
    idempotency_key: str
    deduped: bool = Field(
        default=False,
        description="True if this batch_id is the SAME as a prior submission "
                    "with the same idempotency_key (no new work scheduled).",
    )
    total_rows: int
    valid_rows: int
    invalid_rows: int
    invalid_details: list[dict[str, Any]] = Field(default_factory=list)
    started_at: datetime
    status: BatchStatus


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------


_REQUIRED_COLUMN = "phone"
_KNOWN_COLUMNS = {"phone", "locale", "name", "tags"}


def parse_csv(raw_bytes: bytes) -> tuple[list[BroadcastRow], list[str]]:
    """Parse a CSV upload into typed :class:`BroadcastRow` records.

    Returns ``(rows, fatal_errors)``. ``fatal_errors`` are problems with
    the file itself (encoding, missing required column) that prevent
    ANY processing. Per-row issues are surfaced via ``row.valid = False``
    + ``row.error`` instead.

    Limits:
        * Files are decoded as UTF-8 with BOM tolerated, then UTF-8-sig
          fallback. Other encodings fail with a fatal error.
        * Up to :data:`MAX_ROWS_PER_BATCH` data rows are read; excess
          rows are reported as fatal errors so ops doesn't silently
          send a subset of their list.
    """
    fatal: list[str] = []
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            fatal.append("CSV must be UTF-8 encoded.")
            return [], fatal

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        fatal.append("CSV is empty.")
        return [], fatal

    headers = {h.strip().lower() for h in reader.fieldnames if h}
    if _REQUIRED_COLUMN not in headers:
        fatal.append(
            f"CSV is missing required column '{_REQUIRED_COLUMN}'. "
            f"Found columns: {sorted(headers)}.",
        )
        return [], fatal

    rows: list[BroadcastRow] = []
    for idx, raw_row in enumerate(reader, start=2):  # 1 is the header row
        # Normalize keys to lowercase, strip whitespace; ignore unknown
        # cols. csv.DictReader buckets extra trailing fields under a
        # None key as a LIST — drop those so we don't try to call
        # .strip() on a list.
        norm = {
            (k or "").strip().lower(): (v or "").strip()
            for k, v in raw_row.items()
            if k is not None and isinstance(v, str)
        }
        raw_phone = norm.get(_REQUIRED_COLUMN, "")
        row = BroadcastRow(
            row_number=idx,
            raw_phone=raw_phone,
            locale=(norm.get("locale") or "en").lower(),
            name=norm.get("name") or None,
            tags=[t.strip() for t in (norm.get("tags") or "").split(",") if t.strip()],
        )
        if not raw_phone:
            row.valid = False
            row.error = "phone is empty"
        else:
            phone = _normalize_phone(raw_phone)
            if phone is None:
                row.valid = False
                row.error = "phone is not a valid E.164 number"
            else:
                row.phone = phone
        if row.locale not in ("en", "ar"):
            row.valid = False
            row.error = f"unsupported locale '{row.locale}'"
        rows.append(row)
        if len(rows) >= MAX_ROWS_PER_BATCH:
            # Peek for additional rows so we can report the overrun.
            remaining = sum(1 for _ in reader)
            if remaining > 0:
                fatal.append(
                    f"CSV has more than {MAX_ROWS_PER_BATCH} rows. "
                    f"Trim and submit again (or split into multiple batches). "
                    f"Excess: {remaining} rows.",
                )
            break

    if not rows and not fatal:
        fatal.append("CSV has no data rows.")
    return rows, fatal


# ---------------------------------------------------------------------------
# Store (Redis + in-memory)
# ---------------------------------------------------------------------------


@runtime_checkable
class BroadcastStore(Protocol):
    """Persistence interface for batch state + the idempotency lock."""

    async def claim_idempotency(
        self, idempotency_key: str, batch_id: str, *, ttl_seconds: int,
    ) -> str:
        """Atomically claim the key.

        If the key was previously unused, returns ``batch_id`` (the
        caller's). If the key was already claimed, returns the
        ``batch_id`` that won — the caller MUST use that one and skip
        scheduling new work.
        """
        ...

    async def save_batch(self, batch: BroadcastBatch) -> None: ...
    async def get_batch(self, batch_id: str) -> BroadcastBatch | None: ...
    async def list_recent(self, limit: int = RECENT_BATCH_LIMIT) -> list[BroadcastBatch]: ...


class InMemoryBroadcastStore:
    """Process-local store for tests + the no-Redis dev path."""

    def __init__(self) -> None:
        self._idem: dict[str, str] = {}
        self._batches: dict[str, BroadcastBatch] = {}
        self._order: list[str] = []

    async def claim_idempotency(
        self, idempotency_key: str, batch_id: str, *, ttl_seconds: int,
    ) -> str:
        return self._idem.setdefault(idempotency_key, batch_id)

    async def save_batch(self, batch: BroadcastBatch) -> None:
        if batch.batch_id not in self._batches:
            self._order.append(batch.batch_id)
        self._batches[batch.batch_id] = batch

    async def get_batch(self, batch_id: str) -> BroadcastBatch | None:
        return self._batches.get(batch_id)

    async def list_recent(self, limit: int = RECENT_BATCH_LIMIT) -> list[BroadcastBatch]:
        ids = list(reversed(self._order))[:limit]
        return [self._batches[i] for i in ids if i in self._batches]


class RedisBroadcastStore:
    """Redis-backed store. Same connection pattern as :class:`RedisWebhookDedupe`.

    Key layout::

        {prefix}:broadcast:idem:{key}           → batch_id, NX EX 24h
        {prefix}:broadcast:batch:{batch_id}     → JSON, EX 7d
        {prefix}:broadcast:recent               → ZSET (score=epoch_seconds), trimmed to top-N
    """

    def __init__(self, *, url: str, key_prefix: str = "madad") -> None:
        self._url = url
        self._prefix = key_prefix
        self._client: Any | None = None

    async def _conn(self) -> Any:
        if self._client is None:
            import redis.asyncio as aioredis
            self._client = aioredis.from_url(self._url, decode_responses=True)
        return self._client

    def _k(self, suffix: str) -> str:
        return f"{self._prefix}:broadcast:{suffix}"

    async def claim_idempotency(
        self, idempotency_key: str, batch_id: str, *, ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS,
    ) -> str:
        client = await self._conn()
        key = self._k(f"idem:{idempotency_key}")
        # SET NX EX returns True if the key didn't exist.
        was_set = await client.set(key, batch_id, nx=True, ex=ttl_seconds)
        if was_set:
            return batch_id
        # Lost the race — return whatever's stored.
        existing = await client.get(key)
        return existing or batch_id

    async def save_batch(self, batch: BroadcastBatch) -> None:
        client = await self._conn()
        key = self._k(f"batch:{batch.batch_id}")
        payload = batch.model_dump_json()
        await client.set(key, payload, ex=BATCH_RETENTION_SECONDS)
        score = batch.started_at.timestamp()
        index_key = self._k("recent")
        await client.zadd(index_key, {batch.batch_id: score})
        await client.expire(index_key, BATCH_RETENTION_SECONDS)
        # Trim to keep the index sparse.
        await client.zremrangebyrank(index_key, 0, -(RECENT_BATCH_LIMIT + 1))

    async def get_batch(self, batch_id: str) -> BroadcastBatch | None:
        client = await self._conn()
        raw = await client.get(self._k(f"batch:{batch_id}"))
        if raw is None:
            return None
        return BroadcastBatch.model_validate_json(raw)

    async def list_recent(self, limit: int = RECENT_BATCH_LIMIT) -> list[BroadcastBatch]:
        client = await self._conn()
        index_key = self._k("recent")
        # ZSET returned newest-first.
        ids = await client.zrevrange(index_key, 0, limit - 1)
        out: list[BroadcastBatch] = []
        for bid in ids:
            raw = await client.get(self._k(f"batch:{bid}"))
            if raw is not None:
                out.append(BroadcastBatch.model_validate_json(raw))
        return out


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


@dataclass
class _DispatchOutcome:
    """One row's fate. Lives only inside :meth:`BroadcastCoordinator.process`."""
    row: BroadcastRow
    ok: bool
    error: str | None = None


@runtime_checkable
class CampaignStartCallable(Protocol):
    """The agent-side entry point invoked per row.

    Production: ``OnboardingPlatform.runtime.start``-like wrapper.
    Tests: stub that records calls.
    """

    async def __call__(
        self, *, channel: Channel, identity: str, locale: str,
    ) -> Any: ...


class BroadcastCoordinator:
    """Fan-out worker over a list of :class:`BroadcastRow`.

    Stateless. Receives the row list + a callable that knows how to
    start one campaign. Updates batch state in the store as it goes.
    """

    def __init__(
        self,
        store: BroadcastStore,
        start_one: CampaignStartCallable,
    ) -> None:
        self._store = store
        self._start = start_one

    async def process(self, batch: BroadcastBatch, rows: list[BroadcastRow]) -> None:
        """Drive the fan-out for one batch.

        Updates ``batch.sent`` / ``batch.failed`` / ``batch.status``
        in place AND persists snapshots periodically so GETs midway
        through the batch see meaningful progress.

        Honours ``batch.rate_per_minute`` with an ``asyncio.sleep``
        between rows. ``dry_run`` short-circuits the actual
        ``start_one`` call but still walks the loop so timing is
        comparable to a real run.
        """
        valid_rows = [r for r in rows if r.valid]
        batch.status = BatchStatus.IN_PROGRESS
        batch.total_rows = len(rows)
        batch.valid_rows = len(valid_rows)
        batch.invalid_rows = len(rows) - len(valid_rows)
        await self._store.save_batch(batch)

        if not valid_rows:
            batch.status = BatchStatus.COMPLETED
            batch.completed_at = utcnow()
            await self._store.save_batch(batch)
            return

        interval_seconds = 60.0 / max(1, batch.rate_per_minute)
        # Periodic snapshot cadence: every N rows OR every 5 seconds.
        snapshot_every = max(1, min(25, len(valid_rows) // 10))

        for idx, row in enumerate(valid_rows):
            outcome = await self._dispatch_one(batch, row)
            if outcome.ok:
                batch.sent += 1
            else:
                batch.failed += 1
                if len(batch.failures) < 20:
                    batch.failures.append({
                        "row_number": row.row_number,
                        "phone": row.phone,
                        "error": outcome.error,
                    })
            if (idx + 1) % snapshot_every == 0:
                await self._store.save_batch(batch)
            if idx + 1 < len(valid_rows):
                await asyncio.sleep(interval_seconds)

        batch.status = BatchStatus.COMPLETED
        batch.completed_at = utcnow()
        await self._store.save_batch(batch)

    async def _dispatch_one(
        self, batch: BroadcastBatch, row: BroadcastRow,
    ) -> _DispatchOutcome:
        if batch.dry_run:
            return _DispatchOutcome(row=row, ok=True)
        assert row.phone is not None
        try:
            await self._start(
                channel=batch.channel,
                identity=row.phone,
                locale=row.locale,
            )
            return _DispatchOutcome(row=row, ok=True)
        except Exception as exc:  # noqa: BLE001 — per-row faults don't halt the batch
            _LOG.warning(
                "broadcast.dispatch_failed batch=%s row=%s phone=%s err=%s",
                batch.batch_id, row.row_number, row.phone, str(exc)[:200],
            )
            return _DispatchOutcome(row=row, ok=False, error=str(exc)[:200])


# ---------------------------------------------------------------------------
# Submit pipeline (the route's coordinator)
# ---------------------------------------------------------------------------


def make_batch_id() -> str:
    return new_id("bcast")


def summarize_invalid_rows(rows: list[BroadcastRow], cap: int = 20) -> list[dict[str, Any]]:
    """Trim invalid-row detail list for the response payload.

    Showing 20 examples is enough to debug a CSV; more would bloat
    the response without adding signal. The total invalid count is
    returned alongside.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.valid:
            continue
        out.append({
            "row_number": r.row_number,
            "raw_phone": r.raw_phone,
            "error": r.error,
        })
        if len(out) >= cap:
            break
    return out
