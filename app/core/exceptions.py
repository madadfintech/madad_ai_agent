"""Platform-wide exception hierarchy.

Every deliberate error raised by the platform derives from :class:`AppError`,
giving a stable ``code`` and structured ``details`` that the API layer can map
to a consistent error envelope. Workflow-specific errors live in
``app.shared.workflow.errors`` and extend these.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for all deliberate platform errors.

    ``http_status`` is the response code the API layer maps this error to; it
    lives on the error (not in per-service handler maps) so a single handler can
    serve every service uniformly.
    """

    code: str = "app_error"
    http_status: int = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(AppError):
    code = "validation_error"
    http_status = 422


class NotFoundError(AppError):
    code = "not_found"
    http_status = 404


class ConflictError(AppError):
    code = "conflict"
    http_status = 409


class UpstreamError(AppError):
    """An external dependency (Madad API, MCP, channel provider) failed."""

    code = "upstream_error"
    http_status = 502
