"""Timeout engine: per-step execution budgets and lapsed-session sweeps.

Two distinct timeouts:

* **Step timeout** — a single execution segment may not run longer than its
  budget. Enforced inline with :func:`asyncio.wait_for`.
* **Session/await timeout** — a run waiting for inbound input is considered
  lapsed once ``expires_at`` passes. Surfaced by :meth:`TimeoutEngine.find_expired`
  for a periodic sweep (which the Nudge service / a Celery beat job drives).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import datetime
from typing import TypeVar

from .errors import StepTimeoutError
from .persistence import WorkflowRun, WorkflowRunStore

T = TypeVar("T")


class TimeoutEngine:
    """Enforces step timeouts and locates lapsed waiting runs."""

    async def run(self, awaitable: Awaitable[T], timeout: float | None) -> T:  # noqa: ASYNC109
        """Await ``awaitable`` under a time budget.

        A non-positive or ``None`` timeout means "no budget". On expiry the
        underlying coroutine is cancelled and :class:`StepTimeoutError` is raised.
        """

        if timeout is None or timeout <= 0:
            return await awaitable
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError) as exc:  # noqa: UP041
            raise StepTimeoutError(
                f"Step exceeded its {timeout}s budget",
                details={"timeout": timeout},
            ) from exc

    async def find_expired(
        self, run_store: WorkflowRunStore, now: datetime, limit: int = 100
    ) -> list[WorkflowRun]:
        """Return waiting runs whose ``expires_at`` has passed."""

        return await run_store.list_waiting_expired(now, limit=limit)
