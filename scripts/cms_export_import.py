"""CMS bulk export / import / diff — UAT → prod template promotion (M1).

Vendor Engagement Plan §M1 acceptance bullet: *"Bulk export of UAT
templates / checklists / nudge schedules into a portable manifest that
can be replayed into prod without re-typing every record"*. This is
the script ops runs at the end of M1 dry-run to lift the authored UAT
content into the prod CMS.

Sub-commands
------------

* ``export <out.json>`` — dump every current ``ConfigRecord`` from the
  CMS connected to the running container into a JSON manifest.
* ``import <in.json>`` — read a manifest and upsert each record into
  the target CMS. ``--dry-run`` previews the changes without writing.
* ``diff <in.json>`` — compare a manifest against the live target CMS
  and print added / changed / removed records. Pure read-only.

Filters
-------

``--kind template`` / ``--kind checklist`` / ``--kind nudge`` / etc. —
narrow to a single kind. ``--name-pattern 'onboarding.invoice.*'`` —
narrow to a name glob. Both compose. Default is "everything".

Usage
-----

Inside the running container (same env as ``seed_cms_templates``)::

    # On UAT (after ops authoring complete):
    docker compose exec workflow \
        python -m scripts.cms_export_import export /tmp/uat_cms.json

    # Copy /tmp/uat_cms.json out, scp into prod host, then on prod:
    docker compose exec workflow \
        python -m scripts.cms_export_import diff /tmp/uat_cms.json

    docker compose exec workflow \
        python -m scripts.cms_export_import import /tmp/uat_cms.json --dry-run
    docker compose exec workflow \
        python -m scripts.cms_export_import import /tmp/uat_cms.json

The script writes to whatever PERSISTENCE backend is configured
(Postgres in staging/prod, in-memory in dev). Bring up the stack
first.
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import sys
from pathlib import Path
from typing import Any

from app.services.cms.deps import get_cms_service
from app.services.cms.enums import ConfigKind
from app.services.cms.service import CmsService
from app.shared.i18n import Locale
from app.shared.workflow.enums import Channel

_MANIFEST_VERSION = 1


def _serialize_record(rec: Any) -> dict[str, Any]:
    """Convert a ConfigRecord to the manifest's portable JSON shape.

    We deliberately drop ``version`` / ``version_id`` / ``created_at`` /
    ``updated_at`` / ``updated_by`` — those are environment-local audit
    metadata. The manifest captures the CONTENT (kind, name, channel,
    locale, value) so it round-trips into any environment cleanly.
    """
    return {
        "kind": str(rec.kind),
        "name": rec.name,
        "channel": str(rec.channel) if rec.channel else None,
        "locale": str(rec.locale) if rec.locale else None,
        "value": rec.value,
        "comment": rec.comment,
    }


def _match_filters(
    kind: str, name: str, *,
    kind_filter: str | None,
    name_pattern: str | None,
) -> bool:
    """Return True if this record passes the CLI filters."""
    if kind_filter and kind != kind_filter:
        return False
    if name_pattern and not fnmatch.fnmatch(name, name_pattern):
        return False
    return True


async def _collect_all(
    cms: CmsService,
    *,
    kind_filter: str | None,
    name_pattern: str | None,
) -> list[dict[str, Any]]:
    """Walk every key in the store and return the current records that
    pass the filters, in a stable (kind, name, channel, locale) order."""
    keys = await cms.list_keys()
    out: list[dict[str, Any]] = []
    for key in keys:
        if not _match_filters(
            str(key.kind), key.name,
            kind_filter=kind_filter, name_pattern=name_pattern,
        ):
            continue
        record = await cms.get(
            key.kind, key.name,
            channel=key.channel, locale=key.locale,
            use_cache=False,  # always read the freshest copy for the manifest
        )
        if record is None:
            continue
        out.append(_serialize_record(record))
    out.sort(key=lambda r: (r["kind"], r["name"], r["channel"] or "", r["locale"] or ""))
    return out


def _coerce_optional_channel(s: str | None) -> Channel | None:
    return Channel(s) if s else None


def _coerce_optional_locale(s: str | None) -> Locale | None:
    return Locale(s) if s else None


async def export_cmd(args: argparse.Namespace) -> int:
    cms = get_cms_service()
    records = await _collect_all(
        cms,
        kind_filter=args.kind,
        name_pattern=args.name_pattern,
    )
    manifest = {
        "manifest_version": _MANIFEST_VERSION,
        "source": "cms_export_import",
        "record_count": len(records),
        "records": records,
    }
    out_path = Path(args.out)
    out_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    print(f"  ✓ Exported {len(records)} records → {out_path}")
    return 0


def _read_manifest(in_path: Path) -> list[dict[str, Any]]:
    if not in_path.exists():
        print(f"  ✗ manifest not found: {in_path}", file=sys.stderr)
        sys.exit(2)
    manifest = json.loads(in_path.read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != _MANIFEST_VERSION:
        print(
            f"  ✗ manifest_version mismatch (expected {_MANIFEST_VERSION}, "
            f"got {manifest.get('manifest_version')}). Re-export from the same "
            f"toolchain version.",
            file=sys.stderr,
        )
        sys.exit(2)
    return list(manifest.get("records", []))


async def _classify_changes(
    cms: CmsService, manifest_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Compare manifest records against the live target. Returns
    (added, changed, unchanged) lists.

    "Removed" (in target but not in manifest) is NOT applied or even
    listed by ``diff`` — promotion is purposefully ADDITIVE so a stale
    UAT manifest can't accidentally delete prod-only emergency
    content. Use a dedicated DELETE flow when that's actually wanted.
    """
    added: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    for rec in manifest_records:
        kind = ConfigKind(rec["kind"])
        record = await cms.get(
            kind, rec["name"],
            channel=_coerce_optional_channel(rec.get("channel")),
            locale=_coerce_optional_locale(rec.get("locale")),
            use_cache=False,
        )
        if record is None:
            added.append(rec)
        elif record.value != rec["value"]:
            changed.append(rec)
        else:
            unchanged.append(rec)
    return added, changed, unchanged


async def diff_cmd(args: argparse.Namespace) -> int:
    cms = get_cms_service()
    records = _read_manifest(Path(args.in_path))
    records = [
        r for r in records
        if _match_filters(
            r["kind"], r["name"],
            kind_filter=args.kind, name_pattern=args.name_pattern,
        )
    ]
    added, changed, unchanged = await _classify_changes(cms, records)
    print(f"  Manifest: {len(records)} record(s) after filters")
    print(f"  + added   {len(added)}")
    print(f"  ~ changed {len(changed)}")
    print(f"  = same    {len(unchanged)}")
    for r in added:
        print(f"      + {r['kind']}:{r['name']} (ch={r['channel']} loc={r['locale']})")
    for r in changed:
        print(f"      ~ {r['kind']}:{r['name']} (ch={r['channel']} loc={r['locale']})")
    return 0


async def import_cmd(args: argparse.Namespace) -> int:
    cms = get_cms_service()
    records = _read_manifest(Path(args.in_path))
    records = [
        r for r in records
        if _match_filters(
            r["kind"], r["name"],
            kind_filter=args.kind, name_pattern=args.name_pattern,
        )
    ]
    added, changed, unchanged = await _classify_changes(cms, records)
    print(f"  Manifest: {len(records)} record(s) after filters")
    print(f"  + would-add     {len(added)}")
    print(f"  ~ would-change  {len(changed)}")
    print(f"  = unchanged     {len(unchanged)}")
    if args.dry_run:
        print("  (dry-run — no writes)")
        return 0
    to_apply = added + changed
    applied = 0
    for rec in to_apply:
        await cms.upsert(
            ConfigKind(rec["kind"]),
            rec["name"],
            rec["value"],
            channel=_coerce_optional_channel(rec.get("channel")),
            locale=_coerce_optional_locale(rec.get("locale")),
            comment=rec.get("comment"),
            updated_by=args.updated_by or "cms_export_import",
        )
        applied += 1
        print(f"    ✓ upsert {rec['kind']}:{rec['name']}")
    print(f"  Applied {applied} change(s).")
    return 0


def _add_filters(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--kind",
        choices=[k.value for k in ConfigKind],
        default=None,
        help="Narrow to one kind (template/checklist/nudge/...).",
    )
    p.add_argument(
        "--name-pattern",
        default=None,
        help="fnmatch pattern on record name (e.g. 'onboarding.invoice.*').",
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cms_export_import",
        description="Bulk export / import / diff for CMS template promotion.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export", help="Dump current CMS to a JSON manifest.")
    p_exp.add_argument("out", help="Path to write the manifest JSON.")
    _add_filters(p_exp)

    p_diff = sub.add_parser("diff", help="Compare a manifest to the live CMS (read-only).")
    p_diff.add_argument("in_path", metavar="in", help="Manifest JSON to compare against.")
    _add_filters(p_diff)

    p_imp = sub.add_parser("import", help="Upsert records from a manifest into the live CMS.")
    p_imp.add_argument("in_path", metavar="in", help="Manifest JSON to apply.")
    p_imp.add_argument(
        "--dry-run", action="store_true",
        help="Print classification, write nothing.",
    )
    p_imp.add_argument(
        "--updated-by", default=None,
        help="Audit field — who is running this promotion.",
    )
    _add_filters(p_imp)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "export":
        return asyncio.run(export_cmd(args))
    if args.cmd == "diff":
        return asyncio.run(diff_cmd(args))
    if args.cmd == "import":
        return asyncio.run(import_cmd(args))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
