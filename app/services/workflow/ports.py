"""Business ports the onboarding workflow orchestrates through.

The workflow nodes never touch external systems directly — they call these ports.
Real adapters bridge to the Communication / Document / Nudge services (and, via
MCP, to Madad's APIs and Tess); in-memory fakes drive deterministic tests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Any

from app.shared.workflow.enums import Channel
from app.shared.workflow.utils import new_id

# -- Messenger (outbound conversation via Communication + CMS) ---------------


class Messenger(ABC):
    @abstractmethod
    async def send(
        self,
        *,
        channel: Channel,
        identity: str,
        template_key: str,
        variables: dict[str, Any] | None = None,
        locale: str = "en",
    ) -> None: ...


class RecordingMessenger(Messenger):
    """Records outbound sends (no rendering needed) for tests."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(
        self,
        *,
        channel: Channel,
        identity: str,
        template_key: str,
        variables: dict[str, Any] | None = None,
        locale: str = "en",
    ) -> None:
        self.sent.append(
            {
                "channel": channel,
                "identity": identity,
                "template_key": template_key,
                "variables": variables or {},
                "locale": locale,
            }
        )

    def templates(self) -> list[str]:
        return [s["template_key"] for s in self.sent]


# -- Document intake (via Document Intelligence service) ----------------------


class DocumentIntake(ABC):
    @abstractmethod
    async def ingest(
        self, *, application_ref: str | None, filename: str, provider_ref: str | None = None
    ) -> None: ...

    @abstractmethod
    async def missing(self, *, checklist: str, application_ref: str | None) -> list[str]: ...


class InMemoryDocumentIntake(DocumentIntake):
    """Simulates document processing + checklist tracking for tests.

    Classifies an ingested file by a filename-keyword map and computes the still
    -missing required codes for an application.
    """

    def __init__(self, *, required: list[str], type_by_keyword: dict[str, str]) -> None:
        self._required = required
        self._types = type_by_keyword
        self._received: dict[str, set[str]] = defaultdict(set)

    async def ingest(
        self, *, application_ref: str | None, filename: str, provider_ref: str | None = None
    ) -> None:
        key = application_ref or "_"
        lowered = filename.lower()
        for keyword, doc_type in self._types.items():
            if keyword in lowered:
                self._received[key].add(doc_type)
                break

    async def missing(self, *, checklist: str, application_ref: str | None) -> list[str]:
        received = self._received[application_ref or "_"]
        return [code for code in self._required if code not in received]


# -- Madad backend (via MCP) -------------------------------------------------


class MadadClient(ABC):
    """Madad backend operations (consumed via MCP). Async decisions (pre-qual,
    score, offers, selection) arrive later as webhook resume payloads."""

    @abstractmethod
    async def check_eligibility(self, cr_ref: str | None) -> bool: ...

    @abstractmethod
    async def request_prequalification(self, *, identity: str, cr_ref: str | None) -> str: ...

    @abstractmethod
    async def request_score(self, application_ref: str | None) -> None: ...

    @abstractmethod
    async def submit_to_lenders(self, application_ref: str | None) -> None: ...

    @abstractmethod
    async def activate_credit_line(
        self, application_ref: str | None, offer: dict[str, Any]
    ) -> dict[str, Any]: ...


class InMemoryMadadClient(MadadClient):
    def __init__(self, *, eligible: bool = True) -> None:
        self.eligible = eligible
        self.calls: list[str] = []

    async def check_eligibility(self, cr_ref: str | None) -> bool:
        self.calls.append("check_eligibility")
        return self.eligible

    async def request_prequalification(self, *, identity: str, cr_ref: str | None) -> str:
        self.calls.append("request_prequalification")
        return new_id("madad")

    async def request_score(self, application_ref: str | None) -> None:
        self.calls.append("request_score")

    async def submit_to_lenders(self, application_ref: str | None) -> None:
        self.calls.append("submit_to_lenders")

    async def activate_credit_line(
        self, application_ref: str | None, offer: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append("activate_credit_line")
        return {"active": True, **offer}


# -- Payments (Tess, via MCP) ------------------------------------------------


class PaymentClient(ABC):
    @abstractmethod
    async def create_link(self, *, application_ref: str | None, amount: int) -> str: ...


class InMemoryPaymentClient(PaymentClient):
    def __init__(self) -> None:
        self.links: list[str] = []

    async def create_link(self, *, application_ref: str | None, amount: int) -> str:
        link = f"https://pay.tess.example/{application_ref or 'app'}/{amount}"
        self.links.append(link)
        return link


# -- Reminders (via Nudge service) -------------------------------------------


class Reminders(ABC):
    @abstractmethod
    async def schedule(
        self,
        reason: str,
        *,
        channel: Channel,
        identity: str,
        target_ref: str | None,
        variables: dict[str, Any] | None = None,
    ) -> None: ...

    @abstractmethod
    async def suppress(self, *, target_ref: str | None) -> None: ...


class RecordingReminders(Reminders):
    def __init__(self) -> None:
        self.scheduled: list[str] = []
        self.suppressed: list[str | None] = []

    async def schedule(
        self,
        reason: str,
        *,
        channel: Channel,
        identity: str,
        target_ref: str | None,
        variables: dict[str, Any] | None = None,
    ) -> None:
        self.scheduled.append(reason)

    async def suppress(self, *, target_ref: str | None) -> None:
        self.suppressed.append(target_ref)
