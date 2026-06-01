"""Retry engine with deterministic, injectable backoff.

The executor drives one *segment* of a workflow per invocation (run until the
next interrupt or completion). If a segment fails transiently, the retry engine
re-drives it; because LangGraph checkpoints after every super-step, re-driving
resumes from the last good checkpoint rather than restarting the workflow.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pydantic import BaseModel

from .errors import RetryExhaustedError
from .utils import compute_backoff

T = TypeVar("T")

# (attempt_number, exception, delay_seconds) -> None
OnRetry = Callable[[int, Exception, float], Awaitable[None]]
# Async sleep function (injectable so tests don't actually wait).
SleepFn = Callable[[float], Awaitable[None]]


class RetryPolicy(BaseModel):
    """How a failing segment should be retried."""

    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 30.0
    jitter: bool = True
    # Exception types that must never be retried (e.g. validation errors).
    non_retryable: tuple[type[Exception], ...] = ()

    model_config = {"arbitrary_types_allowed": True}

    def is_retryable(self, exc: Exception) -> bool:
        return not isinstance(exc, self.non_retryable)


class RetryEngine:
    """Runs an attempt-factory under a :class:`RetryPolicy`."""

    def __init__(
        self,
        *,
        sleep: SleepFn | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._sleep: SleepFn = sleep or asyncio.sleep
        self._rng = rng or random.Random()

    async def run(
        self,
        factory: Callable[[int], Awaitable[T]],
        policy: RetryPolicy,
        *,
        on_retry: OnRetry | None = None,
    ) -> T:
        """Invoke ``factory(attempt)`` until it succeeds or attempts are exhausted.

        ``attempt`` is 0-based; the executor uses it to decide whether to send the
        initial input/command (attempt 0) or simply continue from the checkpoint.
        """

        last_exc: Exception | None = None
        attempts = max(1, policy.max_attempts)
        for attempt in range(attempts):
            try:
                return await factory(attempt)
            except Exception as exc:  # noqa: BLE001 - policy decides retryability
                if not policy.is_retryable(exc):
                    raise
                last_exc = exc
                if attempt >= attempts - 1:
                    break
                delay = compute_backoff(
                    attempt + 1,
                    base_delay=policy.base_delay,
                    max_delay=policy.max_delay,
                    jitter=policy.jitter,
                    rng=self._rng,
                )
                if on_retry is not None:
                    await on_retry(attempt + 1, exc, delay)
                await self._sleep(delay)

        raise RetryExhaustedError(
            f"Exhausted {attempts} attempt(s)",
            details={"last_error": str(last_exc)},
        ) from last_exc
