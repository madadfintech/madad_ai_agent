"""Cleanup test users + every byte of their associated data.

Wipes ALL persistence layers for one or more identities:

* Postgres
    - ``workflow.runs`` + ``workflow.run_audit`` (joined via run_id)
    - ``communication.messages`` + ``communication.conversations``
    - ``nudge.reminders`` + ``nudge.sequences``
    - ``public.checkpoints`` + ``public.checkpoint_writes`` +
      ``public.checkpoint_blobs`` (LangGraph checkpoint tables, matched
      via ``thread_id`` joined back to ``workflow.runs`` BEFORE the run
      row is dropped).
* Redis — every key matching ``*<identity>*`` (session pointers,
  webhook dedupe entries, campaign_start locks, etc).

Usage (inside the workflow container — DB DSN + Redis URL come from
the same env vars the workflow service uses)::

    docker compose -f docker/docker-compose.yml --env-file .env exec workflow \\
        python -m scripts.cleanup_test_users +919497191690 +918287611995

    docker compose -f docker/docker-compose.yml --env-file .env exec workflow \\
        python -m scripts.cleanup_test_users --pattern '+91%'

    docker compose -f docker/docker-compose.yml --env-file .env exec workflow \\
        python -m scripts.cleanup_test_users --dry-run +919497191690

Flags
-----
``--dry-run`` Print row counts that WOULD be deleted; touch nothing.
``--pattern`` SQL LIKE pattern matched against ``data->>'identity'``.
              Combine with identities — both are unioned.
``--yes``     Skip the confirmation prompt.

NEVER hard-codes "test" identities — every wipe is explicit. Refuses to
run when neither an identity nor a pattern is supplied (no accidental
"wipe everything" by typo).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

try:
    import redis.asyncio as aioredis  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    aioredis = None


# ---------------------------------------------------------------------------
# Config — reuse the same env vars the workflow service reads
# ---------------------------------------------------------------------------


def _libpq_dsn_from_env() -> str:
    """Translate the asyncpg-style DSN the workflow uses (``+asyncpg``)
    into a plain libpq DSN ``asyncpg.connect`` accepts."""
    dsn = os.environ.get("POSTGRES__DSN") or os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise SystemExit(
            "POSTGRES__DSN not set — run this inside the workflow container "
            "or export the DSN explicitly."
        )
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://"):
        if dsn.startswith(prefix):
            return "postgresql://" + dsn[len(prefix):]
    return dsn


def _redis_url_from_env() -> str | None:
    return os.environ.get("REDIS__URL") or os.environ.get("REDIS_URL") or None


# ---------------------------------------------------------------------------
# Postgres delete plan
# ---------------------------------------------------------------------------


_IDENTITY_CLAUSE = (
    "(data->>'identity' = ANY($1::text[]) "
    "OR ($2::text IS NOT NULL AND data->>'identity' LIKE $2::text))"
)
_NUDGE_CLAUSE = (
    "(data::text LIKE ANY($1::text[]) "
    "OR ($2::text IS NOT NULL AND data::text LIKE $2::text))"
)


# (table label, FROM/WHERE fragment, arg_kind)
# arg_kind = "identity" → pass (identities, pattern)
# arg_kind = "nudge"    → pass (nudge_likes, nudge_pattern)
_TABLES = [
    (
        "public.checkpoint_writes",
        f"FROM public.checkpoint_writes WHERE thread_id IN ("
        f"SELECT thread_id FROM workflow.runs WHERE {_IDENTITY_CLAUSE})",
        "identity",
    ),
    (
        "public.checkpoint_blobs",
        f"FROM public.checkpoint_blobs WHERE thread_id IN ("
        f"SELECT thread_id FROM workflow.runs WHERE {_IDENTITY_CLAUSE})",
        "identity",
    ),
    (
        "public.checkpoints",
        f"FROM public.checkpoints WHERE thread_id IN ("
        f"SELECT thread_id FROM workflow.runs WHERE {_IDENTITY_CLAUSE})",
        "identity",
    ),
    (
        "workflow.run_audit",
        f"FROM workflow.run_audit WHERE run_id IN ("
        f"SELECT run_id FROM workflow.runs WHERE {_IDENTITY_CLAUSE})",
        "identity",
    ),
    ("workflow.runs", f"FROM workflow.runs WHERE {_IDENTITY_CLAUSE}", "identity"),
    (
        "communication.messages",
        f"FROM communication.messages WHERE {_IDENTITY_CLAUSE}",
        "identity",
    ),
    (
        "communication.conversations",
        f"FROM communication.conversations WHERE {_IDENTITY_CLAUSE}",
        "identity",
    ),
    ("nudge.reminders", f"FROM nudge.reminders WHERE {_NUDGE_CLAUSE}", "nudge"),
    ("nudge.sequences", f"FROM nudge.sequences WHERE {_NUDGE_CLAUSE}", "nudge"),
]


async def cleanup_postgres(
    identities: list[str],
    pattern: str | None,
    *,
    dry_run: bool,
) -> dict[str, int]:
    """Run every delete in dependency order. Each statement takes
    exactly 2 args (Postgres rejects extras)."""
    nudge_likes = [f"%{i}%" for i in identities]
    nudge_pattern = f"%{pattern}%" if pattern else None
    args_by_kind = {
        "identity": (identities, pattern),
        "nudge": (nudge_likes, nudge_pattern),
    }
    counts: dict[str, int] = {}
    conn = await asyncpg.connect(_libpq_dsn_from_env())
    try:
        if dry_run:
            for table, fragment, kind in _TABLES:
                args = args_by_kind[kind]
                row = await conn.fetchrow(
                    "SELECT count(*) AS c " + fragment, *args,
                )
                count = int(row["c"]) if row else 0
                counts[table] = count
                print(f"  ✓ {table:<32} would delete {count}")
        else:
            async with conn.transaction():
                for table, fragment, kind in _TABLES:
                    args = args_by_kind[kind]
                    result = await conn.execute("DELETE " + fragment, *args)
                    count = (
                        int(result.split()[-1])
                        if result.startswith("DELETE ") else 0
                    )
                    counts[table] = count
                    print(f"  ✓ {table:<32} deleted {count}")
    finally:
        await conn.close()
    return counts


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


async def cleanup_redis(
    identities: list[str],
    pattern: str | None,
    *,
    dry_run: bool,
) -> dict[str, int]:
    """Drop every Redis key matching ``*<identity>*`` per identity, then
    repeat for the optional pattern. Uses ``KEYS`` since staging has
    small key counts; swap for ``SCAN`` if this ever runs in prod."""
    if aioredis is None:
        print("  ⚠ redis.asyncio not available — skipping Redis cleanup.")
        return {}
    url = _redis_url_from_env()
    if not url:
        print("  ⚠ REDIS__URL not set — skipping Redis cleanup.")
        return {}
    counts: dict[str, int] = {}
    client = aioredis.from_url(url, decode_responses=True)
    try:
        for ident in identities:
            glob = f"*{ident}*"
            keys = await client.keys(glob)
            counts[ident] = len(keys)
            verb = "would delete" if dry_run else "deleted"
            print(f"  ✓ keys matching {glob:<30} {verb} {len(keys)}")
            if keys and not dry_run:
                await client.delete(*keys)
        if pattern:
            # SQL LIKE → Redis glob: % → *, _ → ?
            glob = "*" + pattern.replace("%", "*").replace("_", "?") + "*"
            keys = await client.keys(glob)
            counts["pattern"] = len(keys)
            verb = "would delete" if dry_run else "deleted"
            print(f"  ✓ keys matching {glob:<30} {verb} {len(keys)}")
            if keys and not dry_run:
                await client.delete(*keys)
    finally:
        await client.aclose()
    return counts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Cleanup test users + every byte of their data.",
        epilog=(
            "Examples:\n"
            "  cleanup_test_users +919497191690 +918287611995\n"
            "  cleanup_test_users --pattern '+91%%' --dry-run\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "identities", nargs="*",
        help="One or more identities to wipe (e.g. +919497191690).",
    )
    p.add_argument(
        "--pattern",
        help="SQL LIKE pattern matched against data->>'identity'. "
             "Combine with identities — both are unioned.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print counts that would be deleted; touch nothing.",
    )
    p.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt.",
    )
    return p.parse_args()


async def _main() -> int:
    args = _parse_args()
    if not args.identities and not args.pattern:
        print(
            "Refusing to run with no identities and no pattern. "
            "Pass at least one identity (e.g. +919497191690) or --pattern.",
            file=sys.stderr,
        )
        return 2

    print()
    print("=" * 70)
    print(("DRY RUN" if args.dry_run else "WIPING") + " test-user data:")
    if args.identities:
        print("  identities :", ", ".join(args.identities))
    if args.pattern:
        print("  pattern    :", args.pattern)
    print("=" * 70)

    if not args.dry_run and not args.yes:
        try:
            confirm = input("Proceed? Type 'yes' to confirm: ").strip().lower()
        except EOFError:
            confirm = ""
        if confirm != "yes":
            print("Aborted.")
            return 1

    print()
    print("Postgres")
    print("-" * 70)
    await cleanup_postgres(
        list(args.identities), args.pattern, dry_run=args.dry_run,
    )

    print()
    print("Redis")
    print("-" * 70)
    await cleanup_redis(
        list(args.identities), args.pattern, dry_run=args.dry_run,
    )

    print()
    print("Done." + (" (dry run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
