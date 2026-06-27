"""Vendor Plan M1 acceptance — UAT→prod template promotion.

Tests the export / diff / import round-trip for ``cms_export_import.py``:

  * Export captures the live CMS content with stable order + portable shape.
  * Import is idempotent (re-running with the same manifest = no diff).
  * Diff classifies records as added / changed / unchanged.
  * Filters (``--kind``, ``--name-pattern``) narrow both export and import.
  * The promotion is purposefully ADDITIVE — a record missing from the
    manifest is NOT deleted from the target.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.services.cms import build_cms_service
from app.services.cms.enums import ConfigKind
from app.shared.i18n import Locale
from scripts import cms_export_import as cei


async def _seed_three_templates(cms) -> None:
    await cms.upsert_template(
        "onboarding.invoice.confirm",
        Locale.EN,
        "Invoice {{ amount }} — please confirm.",
        subject="Madad — confirm invoice",
    )
    await cms.upsert_template(
        "onboarding.welcome_back",
        Locale.EN,
        "Welcome back!",
    )
    await cms.upsert_template(
        "onboarding.campaign.intro",
        Locale.EN,
        "Hello {{ name }}, welcome to Madad.",
    )


async def test_export_captures_current_records(tmp_path: Path, monkeypatch) -> None:
    """Export dumps every current record in stable order with portable
    shape — the manifest is what ops emails to themselves to promote."""
    cms = build_cms_service()
    await _seed_three_templates(cms)
    monkeypatch.setattr(cei, "get_cms_service", lambda: cms)

    out_path = tmp_path / "uat_cms.json"
    args = cei._build_parser().parse_args(["export", str(out_path)])
    rc = await cei.export_cmd(args)
    assert rc == 0
    assert out_path.exists()

    manifest = json.loads(out_path.read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 1
    assert manifest["record_count"] == 3
    # Stable order: sorted by (kind, name).
    names = [r["name"] for r in manifest["records"]]
    assert names == sorted(names)
    # Portable shape — only content + identity, no env-local audit metadata.
    rec = manifest["records"][0]
    assert set(rec.keys()) == {"kind", "name", "channel", "locale", "value", "comment"}


async def test_import_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    """Round-trip: export, then import into a FRESH CMS, then export
    again — the second manifest must equal the first. This is the
    contract ops relies on (UAT export → prod import → prod export
    matches UAT)."""
    src_cms = build_cms_service()
    await _seed_three_templates(src_cms)
    monkeypatch.setattr(cei, "get_cms_service", lambda: src_cms)

    out_path = tmp_path / "uat.json"
    args = cei._build_parser().parse_args(["export", str(out_path)])
    await cei.export_cmd(args)
    src_manifest = json.loads(out_path.read_text())

    dst_cms = build_cms_service()
    monkeypatch.setattr(cei, "get_cms_service", lambda: dst_cms)
    args = cei._build_parser().parse_args(["import", str(out_path)])
    rc = await cei.import_cmd(args)
    assert rc == 0

    # Re-export and compare — the manifests must match record-for-record.
    out_dst = tmp_path / "prod.json"
    args = cei._build_parser().parse_args(["export", str(out_dst)])
    await cei.export_cmd(args)
    dst_manifest = json.loads(out_dst.read_text())
    assert dst_manifest["records"] == src_manifest["records"]


async def test_diff_classifies_added_changed_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    """Diff against a populated target classifies records into added /
    changed / unchanged. Drives the "preview before apply" step ops
    runs against prod before promoting UAT content."""
    src_cms = build_cms_service()
    await _seed_three_templates(src_cms)
    monkeypatch.setattr(cei, "get_cms_service", lambda: src_cms)
    out_path = tmp_path / "uat.json"
    await cei.export_cmd(cei._build_parser().parse_args(["export", str(out_path)]))

    # Build the target with: one record matching, one record with a
    # different body, one record missing (will be "added" by the import).
    dst_cms = build_cms_service()
    await dst_cms.upsert_template(
        "onboarding.welcome_back", Locale.EN, "Welcome back!"
    )  # exact match — unchanged
    await dst_cms.upsert_template(
        "onboarding.campaign.intro", Locale.EN, "Different intro"
    )  # changed
    # invoice.confirm intentionally absent — will be added
    monkeypatch.setattr(cei, "get_cms_service", lambda: dst_cms)

    records = cei._read_manifest(out_path)
    added, changed, unchanged = await cei._classify_changes(dst_cms, records)
    by_name = lambda lst: sorted(r["name"] for r in lst)  # noqa: E731
    assert by_name(added) == ["onboarding.invoice.confirm"]
    assert by_name(changed) == ["onboarding.campaign.intro"]
    assert by_name(unchanged) == ["onboarding.welcome_back"]


async def test_import_is_additive_not_destructive(
    tmp_path: Path, monkeypatch
) -> None:
    """A record present in the TARGET but absent from the MANIFEST must
    NOT be deleted by import. M1 contract: promotion is additive so a
    stale UAT manifest can't wipe prod-only emergency content."""
    src_cms = build_cms_service()
    await src_cms.upsert_template(
        "onboarding.welcome_back", Locale.EN, "Hi again"
    )
    monkeypatch.setattr(cei, "get_cms_service", lambda: src_cms)
    out_path = tmp_path / "uat.json"
    await cei.export_cmd(cei._build_parser().parse_args(["export", str(out_path)]))

    # Target has both records; manifest will only cover one.
    dst_cms = build_cms_service()
    await dst_cms.upsert_template(
        "prod.emergency_banner", Locale.EN, "🚨 Pls do not delete me",
    )
    monkeypatch.setattr(cei, "get_cms_service", lambda: dst_cms)
    args = cei._build_parser().parse_args(["import", str(out_path)])
    await cei.import_cmd(args)

    # Emergency banner survives — it was never touched.
    survived = await dst_cms.get_template_body(
        "prod.emergency_banner", Locale.EN,
    )
    assert survived == "🚨 Pls do not delete me"


async def test_dry_run_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    """``import --dry-run`` prints classification but writes nothing,
    so ops can preview a promotion before authorising it."""
    src_cms = build_cms_service()
    await src_cms.upsert_template(
        "onboarding.welcome_back", Locale.EN, "Hi again",
    )
    monkeypatch.setattr(cei, "get_cms_service", lambda: src_cms)
    out_path = tmp_path / "uat.json"
    await cei.export_cmd(cei._build_parser().parse_args(["export", str(out_path)]))

    dst_cms = build_cms_service()
    monkeypatch.setattr(cei, "get_cms_service", lambda: dst_cms)
    args = cei._build_parser().parse_args(["import", str(out_path), "--dry-run"])
    await cei.import_cmd(args)

    # Dry-run wrote nothing — the welcome_back template doesn't exist on dst.
    body = await dst_cms.get_template_body("onboarding.welcome_back", Locale.EN)
    assert body is None


async def test_filter_by_kind_and_name_pattern(
    tmp_path: Path, monkeypatch
) -> None:
    """``--kind template --name-pattern 'onboarding.invoice.*'`` narrows
    the export so a partial promotion can be done without dragging
    everything along."""
    cms = build_cms_service()
    await _seed_three_templates(cms)
    # Add an unrelated nudge schedule + a non-invoice template to filter out.
    await cms.upsert(
        ConfigKind.NUDGE, "financials_pending",
        {
            "schedule": [
                {"offset": 86400, "channels": ["whatsapp"],
                 "template_key": "nudge.financials_pending.1"},
            ],
            "max_attempts": 1,
        },
    )
    monkeypatch.setattr(cei, "get_cms_service", lambda: cms)

    out_path = tmp_path / "filtered.json"
    args = cei._build_parser().parse_args([
        "export", str(out_path),
        "--kind", "template",
        "--name-pattern", "onboarding.invoice.*",
    ])
    await cei.export_cmd(args)
    manifest = json.loads(out_path.read_text(encoding="utf-8"))
    names = sorted(r["name"] for r in manifest["records"])
    assert names == ["onboarding.invoice.confirm"]
    assert all(r["kind"] == "template" for r in manifest["records"])
