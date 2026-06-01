"""RedisConfigCache cross-instance invalidation (verified with an in-memory broker)."""

from __future__ import annotations

from collections.abc import Callable

from app.services.cms.cache_redis import Broadcaster, KvStore, RedisConfigCache
from app.services.cms.enums import ConfigKind
from app.services.cms.models import ConfigRecord


class FakeKv(KvStore):
    """A single shared L2 store (stands in for one Redis)."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ttl: float) -> None:
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


class FakeBroker:
    """A shared pub/sub broker fanning messages to every subscribed instance."""

    def __init__(self) -> None:
        self._handlers: list[Callable[[str], None]] = []

    def endpoint(self) -> Broadcaster:
        broker = self

        class _B(Broadcaster):
            async def publish(self, message: str) -> None:
                for handler in list(broker._handlers):
                    handler(message)

            def subscribe(self, handler: Callable[[str], None]) -> None:
                broker._handlers.append(handler)

        return _B()


def _record(value: dict) -> ConfigRecord:
    from app.shared.workflow.utils import utcnow

    return ConfigRecord(
        kind=ConfigKind.SETTING,
        name="flag",
        version=1,
        value=value,
        created_at=utcnow(),
        updated_at=utcnow(),
    )


async def test_write_on_one_instance_evicts_l1_on_another():
    kv = FakeKv()
    broker = FakeBroker()
    clock = {"t": 0.0}
    a = RedisConfigCache(
        kv=kv, broadcaster=broker.endpoint(), ttl_seconds=999, time_fn=lambda: clock["t"]
    )
    b = RedisConfigCache(
        kv=kv, broadcaster=broker.endpoint(), ttl_seconds=999, time_fn=lambda: clock["t"]
    )

    # Both instances cache v1 in their L1.
    await a.set("cms:flag", _record({"on": False}))
    assert (await b.get("cms:flag")).value == {"on": False}  # b populated L1 from L2

    # A write on instance A invalidates everywhere (store is the source of truth).
    await a.invalidate("cms:flag")
    # B must NOT serve the stale v1 from its L1 — the pub/sub evicted it.
    assert await b.get("cms:flag") is None

    # The next write repopulates L2; B reads the fresh value.
    await a.set("cms:flag", _record({"on": True}))
    assert (await b.get("cms:flag")).value == {"on": True}


async def test_ttl_expiry_falls_back_to_l2():
    kv = FakeKv()
    broker = FakeBroker()
    clock = {"t": 0.0}
    cache = RedisConfigCache(
        kv=kv, broadcaster=broker.endpoint(), ttl_seconds=10, time_fn=lambda: clock["t"]
    )
    await cache.set("cms:flag", _record({"v": 1}))
    # Out-of-band L2 change (another instance) without notifying this one.
    kv.data["cms:flag"] = _record({"v": 2}).model_dump_json()
    assert (await cache.get("cms:flag")).value == {"v": 1}  # still L1-cached
    clock["t"] = 20
    assert (await cache.get("cms:flag")).value == {"v": 2}  # TTL expired -> L2
