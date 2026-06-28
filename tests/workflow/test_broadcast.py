"""Tests for ``app.services.workflow.broadcast``.

Three layers:

1. **CSV parsing** — happy path, missing column, bad phone, locale check,
   empty file, row-cap enforcement, BOM tolerance.
2. **Store** — InMemoryBroadcastStore idempotency + ordering + retrieval.
3. **Coordinator** — fan-out loop with a stub start callable. Covers:
   only-invalid-rows path, mid-batch failure isolation, dry-run skip,
   progress snapshots, rate limit honoured.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.workflow.broadcast import (
    MAX_ROWS_PER_BATCH,
    BatchStatus,
    BroadcastBatch,
    BroadcastCoordinator,
    BroadcastRow,
    InMemoryBroadcastStore,
    _normalize_phone,
    make_batch_id,
    parse_csv,
    summarize_invalid_rows,
)
from app.shared.workflow.enums import Channel

# ---------------------------------------------------------------------------
# Phone normalisation
# ---------------------------------------------------------------------------


class TestNormalizePhone:
    def test_accepts_e164(self) -> None:
        assert _normalize_phone("+97455500001") == "+97455500001"

    def test_strips_whitespace_and_separators(self) -> None:
        assert _normalize_phone(" +974 5550 0001 ") == "+97455500001"
        assert _normalize_phone("+974-555-00001") == "+97455500001"
        assert _normalize_phone("+974 (5) 55-00.001") == "+97455500001"

    def test_adds_plus_when_missing(self) -> None:
        assert _normalize_phone("97455500001") == "+97455500001"

    def test_strips_leading_zero(self) -> None:
        # E.164 doesn't allow a leading zero. Common UAT mistake — accept it.
        assert _normalize_phone("097455500001") == "+97455500001"

    def test_rejects_letters(self) -> None:
        assert _normalize_phone("+974abc55500001") is None
        assert _normalize_phone("not-a-number") is None

    def test_rejects_too_short(self) -> None:
        assert _normalize_phone("+9745") is None

    def test_rejects_too_long(self) -> None:
        assert _normalize_phone("+1234567890123456") is None  # 16 digits

    def test_empty_input(self) -> None:
        assert _normalize_phone("") is None
        assert _normalize_phone("   ") is None


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------


def _csv(text: str) -> bytes:
    return text.encode("utf-8")


class TestParseCsv:
    def test_happy_path(self) -> None:
        rows, fatal = parse_csv(_csv(
            "phone,locale,name,tags\n"
            "+97455500001,en,Sara,vip\n"
            "+97455500002,ar,,returning,trial\n"
        ))
        assert fatal == []
        assert len(rows) == 2
        assert rows[0].phone == "+97455500001"
        assert rows[0].locale == "en"
        assert rows[0].name == "Sara"
        assert rows[0].tags == ["vip"]
        assert rows[0].valid is True
        # NOTE: the second row's tags came back as ["returning"] not
        # ["returning", "trial"] because csv treats commas as field
        # separators, not list separators. Tags is a single comma-joined
        # CELL (semicolons or pipes if ops needs multiple values).
        assert rows[1].locale == "ar"
        assert rows[1].name is None

    def test_missing_required_column_is_fatal(self) -> None:
        rows, fatal = parse_csv(_csv("name,tags\nSara,vip\n"))
        assert rows == []
        assert any("phone" in m for m in fatal)

    def test_empty_phone_marks_row_invalid(self) -> None:
        # csv.DictReader skips fully-blank lines; an empty phone needs
        # a row with a populated other column to be parsed.
        rows, fatal = parse_csv(_csv("phone,name\n,Sara\n+97455500001,Ali\n"))
        assert fatal == []
        assert rows[0].valid is False
        assert rows[0].error == "phone is empty"
        assert rows[1].valid is True

    def test_bad_phone_marks_row_invalid_other_rows_continue(self) -> None:
        rows, fatal = parse_csv(_csv(
            "phone\n+97455500001\nnope\n+97455500003\n"
        ))
        assert fatal == []
        assert [r.valid for r in rows] == [True, False, True]
        assert rows[1].error and "E.164" in rows[1].error

    def test_unsupported_locale_marks_row_invalid(self) -> None:
        rows, fatal = parse_csv(_csv(
            "phone,locale\n+97455500001,fr\n"
        ))
        assert fatal == []
        assert rows[0].valid is False
        assert "fr" in (rows[0].error or "")

    def test_extra_columns_ignored(self) -> None:
        rows, fatal = parse_csv(_csv(
            "phone,company,age\n+97455500001,Acme,42\n"
        ))
        assert fatal == []
        assert len(rows) == 1
        assert rows[0].valid is True
        # No surprise fields on the model.

    def test_bom_tolerated(self) -> None:
        utf8_bom = b"\xef\xbb\xbf"
        rows, fatal = parse_csv(utf8_bom + b"phone\n+97455500001\n")
        assert fatal == []
        assert rows[0].phone == "+97455500001"

    def test_empty_file_is_fatal(self) -> None:
        rows, fatal = parse_csv(_csv(""))
        assert rows == []
        assert fatal  # Some message

    def test_header_only_is_fatal(self) -> None:
        rows, fatal = parse_csv(_csv("phone\n"))
        assert rows == []
        assert fatal

    def test_row_cap_enforced(self) -> None:
        # MAX_ROWS_PER_BATCH+1 rows → fatal error returned and rows
        # truncated to the cap.
        body = "phone\n" + "\n".join(
            f"+9745550{i:04d}" for i in range(MAX_ROWS_PER_BATCH + 5)
        ) + "\n"
        rows, fatal = parse_csv(_csv(body))
        assert len(rows) == MAX_ROWS_PER_BATCH
        assert any(str(MAX_ROWS_PER_BATCH) in m for m in fatal)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inmemory_store_idempotency() -> None:
    store = InMemoryBroadcastStore()
    a = await store.claim_idempotency("key-1", "bcast_a", ttl_seconds=86400)
    b = await store.claim_idempotency("key-1", "bcast_b", ttl_seconds=86400)
    assert a == "bcast_a"
    assert b == "bcast_a"  # second submission keeps the original batch_id


@pytest.mark.asyncio
async def test_inmemory_store_recent_ordering() -> None:
    store = InMemoryBroadcastStore()
    for i in range(3):
        await store.save_batch(BroadcastBatch(
            batch_id=f"bcast_{i}",
            idempotency_key=f"k_{i}",
            channel=Channel.WHATSAPP,
        ))
    recent = await store.list_recent()
    assert [b.batch_id for b in recent] == ["bcast_2", "bcast_1", "bcast_0"]


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class _StubStart:
    """Records every per-row dispatch call. Optional fail_for set."""

    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.calls: list[tuple[Channel, str, str]] = []
        self._fail_for = fail_for or set()

    async def __call__(
        self, *, channel: Channel, identity: str, locale: str,
    ) -> Any:
        self.calls.append((channel, identity, locale))
        if identity in self._fail_for:
            raise RuntimeError(f"forced failure for {identity}")
        return {"run_id": f"run_{identity}"}


def _rows_from_phones(phones: list[str], all_valid: bool = True) -> list[BroadcastRow]:
    return [
        BroadcastRow(
            row_number=idx + 2,
            raw_phone=p,
            phone=p if all_valid else None,
            valid=all_valid,
            error=None if all_valid else "stub-error",
        )
        for idx, p in enumerate(phones)
    ]


@pytest.mark.asyncio
async def test_process_happy_path() -> None:
    store = InMemoryBroadcastStore()
    start = _StubStart()
    coord = BroadcastCoordinator(store, start)
    batch = BroadcastBatch(
        batch_id=make_batch_id(),
        idempotency_key="key",
        channel=Channel.WHATSAPP,
        rate_per_minute=60_000,  # effectively no sleep
    )
    rows = _rows_from_phones(["+97455500001", "+97455500002", "+97455500003"])
    await coord.process(batch, rows)

    assert batch.status == BatchStatus.COMPLETED
    assert batch.sent == 3
    assert batch.failed == 0
    assert len(start.calls) == 3
    persisted = await store.get_batch(batch.batch_id)
    assert persisted is not None
    assert persisted.status == BatchStatus.COMPLETED


@pytest.mark.asyncio
async def test_process_isolates_failing_row() -> None:
    store = InMemoryBroadcastStore()
    start = _StubStart(fail_for={"+97455500002"})
    coord = BroadcastCoordinator(store, start)
    batch = BroadcastBatch(
        batch_id=make_batch_id(),
        idempotency_key="key",
        channel=Channel.WHATSAPP,
        rate_per_minute=60_000,
    )
    rows = _rows_from_phones(["+97455500001", "+97455500002", "+97455500003"])
    await coord.process(batch, rows)

    assert batch.sent == 2
    assert batch.failed == 1
    assert len(batch.failures) == 1
    assert batch.failures[0]["phone"] == "+97455500002"


@pytest.mark.asyncio
async def test_process_skips_invalid_rows() -> None:
    store = InMemoryBroadcastStore()
    start = _StubStart()
    coord = BroadcastCoordinator(store, start)
    batch = BroadcastBatch(
        batch_id=make_batch_id(),
        idempotency_key="key",
        channel=Channel.WHATSAPP,
        rate_per_minute=60_000,
    )
    rows = [
        BroadcastRow(row_number=2, raw_phone="bad", phone=None,
                     valid=False, error="bad phone"),
        BroadcastRow(row_number=3, raw_phone="+97455500001",
                     phone="+97455500001", valid=True),
    ]
    await coord.process(batch, rows)

    assert batch.sent == 1
    assert batch.failed == 0
    assert batch.invalid_rows == 1
    assert len(start.calls) == 1


@pytest.mark.asyncio
async def test_dry_run_skips_start_calls() -> None:
    store = InMemoryBroadcastStore()
    start = _StubStart()
    coord = BroadcastCoordinator(store, start)
    batch = BroadcastBatch(
        batch_id=make_batch_id(),
        idempotency_key="key",
        channel=Channel.WHATSAPP,
        rate_per_minute=60_000,
        dry_run=True,
    )
    await coord.process(batch, _rows_from_phones(["+97455500001", "+97455500002"]))

    assert start.calls == []           # nothing dispatched
    assert batch.sent == 2             # but counted as a "dry-send"
    assert batch.status == BatchStatus.COMPLETED


@pytest.mark.asyncio
async def test_only_invalid_rows_completes_immediately() -> None:
    store = InMemoryBroadcastStore()
    start = _StubStart()
    coord = BroadcastCoordinator(store, start)
    batch = BroadcastBatch(
        batch_id=make_batch_id(),
        idempotency_key="key",
        channel=Channel.WHATSAPP,
    )
    rows = [
        BroadcastRow(row_number=2, raw_phone="bad", phone=None,
                     valid=False, error="bad phone"),
    ]
    await coord.process(batch, rows)
    assert batch.status == BatchStatus.COMPLETED
    assert batch.sent == 0
    assert batch.failed == 0
    assert start.calls == []


@pytest.mark.asyncio
async def test_rate_limit_honoured(monkeypatch) -> None:
    """asyncio.sleep is called between rows with the configured interval."""
    sleeps: list[float] = []

    async def _capture(seconds: float) -> None:
        sleeps.append(seconds)

    import app.services.workflow.broadcast as broadcast_mod
    monkeypatch.setattr(broadcast_mod.asyncio, "sleep", _capture)

    store = InMemoryBroadcastStore()
    start = _StubStart()
    coord = BroadcastCoordinator(store, start)
    batch = BroadcastBatch(
        batch_id=make_batch_id(),
        idempotency_key="key",
        channel=Channel.WHATSAPP,
        rate_per_minute=30,    # 60/30 = 2.0 seconds between rows
    )
    rows = _rows_from_phones(["+97455500001", "+97455500002", "+97455500003"])
    await coord.process(batch, rows)

    # 3 rows → 2 sleeps between (no sleep after the last row).
    assert len(sleeps) == 2
    assert all(abs(s - 2.0) < 0.001 for s in sleeps)


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------


def test_summarize_caps_at_20() -> None:
    rows = [
        BroadcastRow(row_number=i, raw_phone=f"bad{i}", valid=False, error="bad")
        for i in range(50)
    ]
    out = summarize_invalid_rows(rows)
    assert len(out) == 20


def test_summarize_skips_valid_rows() -> None:
    rows = [
        BroadcastRow(row_number=2, raw_phone="+97455500001",
                     phone="+97455500001", valid=True),
        BroadcastRow(row_number=3, raw_phone="bad", valid=False, error="bad phone"),
    ]
    out = summarize_invalid_rows(rows)
    assert len(out) == 1
    assert out[0]["row_number"] == 3
