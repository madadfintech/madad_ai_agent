"""Versioning, rollback, validation, audit, and events."""

from __future__ import annotations

import pytest

from app.services.cms import (
    CmsEventType,
    ConfigKind,
    build_cms_service,
)
from app.services.cms.errors import ConfigValidationError, ConfigVersionNotFoundError

SETTING = ConfigKind.SETTING


async def test_upsert_increments_versions(cms):
    v1 = await cms.upsert(SETTING, "feature", {"enabled": False})
    v2 = await cms.upsert(SETTING, "feature", {"enabled": True})
    assert v1.version == 1
    assert v2.version == 2

    versions = await cms.list_versions(SETTING, "feature")
    assert [v.version for v in versions] == [1, 2]

    current = await cms.get(SETTING, "feature")
    assert current.version == 2
    assert current.value == {"enabled": True}


async def test_rollback_restores_prior_value_as_new_version(cms):
    await cms.upsert(SETTING, "feature", {"enabled": False})  # v1
    await cms.upsert(SETTING, "feature", {"enabled": True})  # v2

    rolled = await cms.rollback(SETTING, "feature", 1)
    assert rolled.version == 3  # append-only history
    assert rolled.value == {"enabled": False}

    current = await cms.get(SETTING, "feature")
    assert current.value == {"enabled": False}
    assert CmsEventType.CONFIG_ROLLED_BACK in [e.type for e in cms._events.history]


async def test_rollback_unknown_version_raises(cms):
    await cms.upsert(SETTING, "feature", {"enabled": True})
    with pytest.raises(ConfigVersionNotFoundError):
        await cms.rollback(SETTING, "feature", 99)


async def test_delete_removes_current_keeps_history(cms):
    await cms.upsert(SETTING, "temp", {"x": 1})
    assert await cms.delete(SETTING, "temp") is True
    assert await cms.get(SETTING, "temp") is None
    # History retained for audit.
    assert len(await cms.list_versions(SETTING, "temp")) == 1


async def test_validation_rejects_bad_template(cms):
    with pytest.raises(ConfigValidationError):
        await cms.upsert_template("welcome", locale=cms._default_locale, body="   ")


async def test_validation_rejects_undeclared_variable(cms):
    with pytest.raises(ConfigValidationError):
        await cms.upsert_template(
            "welcome",
            locale=cms._default_locale,
            body="Hi {{ name }} and {{ unknown }}",
            variables=["name"],
        )


async def test_validation_rejects_duplicate_checklist_codes():
    from app.services.cms import ChecklistItem

    cms = build_cms_service()
    with pytest.raises(ConfigValidationError):
        await cms.upsert_checklist(
            "onboarding",
            [ChecklistItem(code="cr"), ChecklistItem(code="cr")],
        )


async def test_audit_records_changes(cms):
    await cms.upsert(SETTING, "feature", {"enabled": True}, updated_by="pm@madad")
    entries = await cms._audit.list_entries(name="feature")
    assert any(e.action == "upsert" and e.updated_by == "pm@madad" for e in entries)
