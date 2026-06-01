"""Cache behaviour — the <5-minute propagation guarantee.

Two mechanisms are tested: (1) a write invalidates the cache so the next read is
fresh (instant propagation), and (2) a short TTL means even an out-of-band change
propagates within the TTL window.
"""

from __future__ import annotations

from app.services.cms import (
    ConfigKind,
    InMemoryConfigCache,
    InMemoryConfigStore,
    build_cms_service,
)
from app.services.cms.models import ConfigKey

SETTING = ConfigKind.SETTING


async def test_write_invalidates_cache_immediately(cms):
    await cms.upsert(SETTING, "x", {"v": 1})
    first = await cms.get(SETTING, "x")  # caches v1
    assert first.value == {"v": 1}

    await cms.upsert(SETTING, "x", {"v": 2})  # invalidates
    second = await cms.get(SETTING, "x")
    assert second.value == {"v": 2}


async def test_ttl_expiry_propagates_out_of_band_change():
    clock = {"t": 0.0}
    store = InMemoryConfigStore()
    cache = InMemoryConfigCache(ttl_seconds=10.0, time_fn=lambda: clock["t"])
    cms = build_cms_service(store=store, cache=cache)

    await cms.upsert(SETTING, "x", {"v": 1})
    assert (await cms.get(SETTING, "x")).value == {"v": 1}  # caches

    # Change written directly to the store (bypassing cache invalidation),
    # simulating another instance's write.
    await store.save_new_version(ConfigKey(SETTING, "x"), {"v": 2})

    # Still cached within the TTL window.
    assert (await cms.get(SETTING, "x")).value == {"v": 1}

    # Past the TTL, the cache reloads from the store.
    clock["t"] = 20.0
    assert (await cms.get(SETTING, "x")).value == {"v": 2}


async def test_refresh_clears_cache(cms):
    await cms.upsert(SETTING, "x", {"v": 1})
    await cms.get(SETTING, "x")  # cache
    await cms.refresh()
    # After refresh the cache is empty; a read repopulates from the store.
    assert (await cms.get(SETTING, "x")).value == {"v": 1}


async def test_new_checklist_document_reflects_immediately(cms):
    """Mirrors the Milestone 1 test: add a document, agent sees it next read."""

    from app.services.cms import ChecklistItem

    await cms.upsert_checklist("onboarding", [ChecklistItem(code="trade_license")])
    assert [i.code for i in await cms.get_checklist("onboarding")] == ["trade_license"]

    await cms.upsert_checklist(
        "onboarding",
        [ChecklistItem(code="trade_license"), ChecklistItem(code="tax_card")],
    )
    codes = [i.code for i in await cms.get_checklist("onboarding")]
    assert codes == ["trade_license", "tax_card"]
