"""Centralized structured logging built on ``structlog``.

Every log line is enriched with whatever correlation context is bound to the
current async task (correlation id, session id, workflow, run id). This is the
single logging entry point for the whole platform — services and the workflow
runtime both call :func:`get_logger`.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog
from structlog.contextvars import (
    bind_contextvars,
    clear_contextvars,
    merge_contextvars,
    unbind_contextvars,
)

_CONFIGURED = False


def configure_logging(level: str = "INFO", *, json_logs: bool = False) -> None:
    """Configure structlog + stdlib logging. Idempotent.

    ``json_logs`` selects the production JSON renderer (shipped to OpenSearch);
    otherwise a colourised console renderer is used for local development.
    """

    global _CONFIGURED

    shared_processors: list[Any] = [
        merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, sqlalchemy, etc.) through the same stream.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.getLevelNamesMapping().get(level.upper(), logging.INFO),
    )

    _CONFIGURED = True


def get_logger(name: str | None = None) -> Any:
    """Return a bound structlog logger, configuring on first use."""

    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name)


def bind_log_context(**kwargs: Any) -> None:
    """Bind key/values onto the current task's logging context."""

    bind_contextvars(**{k: v for k, v in kwargs.items() if v is not None})


def clear_log_context() -> None:
    clear_contextvars()


@contextmanager
def log_context(**kwargs: Any) -> Iterator[None]:
    """Scope a set of correlation fields to a block, restoring on exit."""

    keys = [k for k, v in kwargs.items() if v is not None]
    bind_log_context(**kwargs)
    try:
        yield
    finally:
        if keys:
            unbind_contextvars(*keys)
