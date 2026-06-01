"""Checkpointer provider abstraction.

LangGraph persists each run's internal channel values via a checkpointer keyed by
``thread_id``. This is what makes pause/resume and crash recovery possible. The
provider port lets the runtime swap between the in-memory saver (dev/tests) and
the Postgres saver (production, ``workflow`` schema) without the executor caring.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver


class CheckpointerProvider(ABC):
    """Port that yields a LangGraph ``BaseCheckpointSaver``."""

    @abstractmethod
    async def setup(self) -> None:
        """Perform any one-time setup (e.g. create checkpoint tables)."""

    @abstractmethod
    def get(self) -> Any:
        """Return the LangGraph checkpointer instance."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release any held resources (connection pools, etc.)."""


class InMemoryCheckpointerProvider(CheckpointerProvider):
    """Process-local checkpointer. State is lost on restart — dev/tests only."""

    def __init__(self) -> None:
        self._saver = InMemorySaver()

    async def setup(self) -> None:
        return None

    def get(self) -> Any:
        return self._saver

    async def aclose(self) -> None:
        return None
