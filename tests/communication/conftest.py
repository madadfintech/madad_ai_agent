"""Fixtures for communication service tests (all in-memory, no real I/O)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from app.services.communication import (
    CommunicationConfig,
    CommunicationService,
    InMemoryCommunicationEventBus,
    InMemoryCommunicationGateway,
    InMemoryTemplateProvider,
    build_communication_service,
)


async def _no_sleep(_delay: float) -> None:
    return None


@dataclass
class Harness:
    service: CommunicationService
    events: InMemoryCommunicationEventBus
    gateway: InMemoryCommunicationGateway
    templates: InMemoryTemplateProvider

    def event_types(self) -> list[str]:
        return [str(e.type) for e in self.events.history]


@pytest.fixture
def make_harness() -> Callable[..., Harness]:
    def _make(*, fail_times: int = 0, config: CommunicationConfig | None = None) -> Harness:
        events = InMemoryCommunicationEventBus()
        gateway = InMemoryCommunicationGateway(fail_times=fail_times)
        templates = InMemoryTemplateProvider()
        cfg = config or CommunicationConfig(retry_base_delay=0.0, retry_jitter=False)
        service = build_communication_service(
            gateway=gateway,
            template_provider=templates,
            events=events,
            config=cfg,
            sleep=_no_sleep,
        )
        return Harness(service=service, events=events, gateway=gateway, templates=templates)

    return _make


@pytest.fixture
def harness(make_harness: Callable[..., Harness]) -> Harness:
    return make_harness()
