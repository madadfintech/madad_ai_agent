"""Fixtures for Document Intelligence tests (in-memory; routes to a fake Madad)."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from app.services.document import (
    DocumentConfig,
    DocumentIntelligenceService,
    InMemoryChecklistProvider,
    InMemoryDocumentEventBus,
    InMemoryMadadDocumentGateway,
    build_document_service,
)


async def _no_sleep(_delay: float) -> None:
    return None


@dataclass
class Harness:
    service: DocumentIntelligenceService
    gateway: InMemoryMadadDocumentGateway
    checklist: InMemoryChecklistProvider
    events: InMemoryDocumentEventBus

    def event_types(self) -> list[str]:
        return [str(e.type) for e in self.events.history]


@pytest.fixture
def make_harness() -> Callable[..., Harness]:
    def _make(
        *, gateway_fail: int = 0, types: dict[str, str] | None = None, max_attempts: int = 3
    ) -> Harness:
        gateway = InMemoryMadadDocumentGateway(
            fail_times=gateway_fail, type_by_keyword=types or {}
        )
        checklist = InMemoryChecklistProvider()
        events = InMemoryDocumentEventBus()
        service = build_document_service(
            gateway=gateway,
            checklist_provider=checklist,
            events=events,
            config=DocumentConfig(
                max_attempts=max_attempts, retry_base_delay=0.0, retry_jitter=False
            ),
            sleep=_no_sleep,
        )
        return Harness(service, gateway, checklist, events)

    return _make


@pytest.fixture
def harness(make_harness: Callable[..., Harness]) -> Harness:
    return make_harness()


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()
