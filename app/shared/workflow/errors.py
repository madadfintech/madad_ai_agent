"""Workflow runtime exception hierarchy."""

from __future__ import annotations

from app.core.exceptions import AppError


class WorkflowError(AppError):
    """Base class for workflow runtime errors."""

    code = "workflow_error"


class WorkflowNotFoundError(WorkflowError):
    code = "workflow_not_found"
    http_status = 404


class WorkflowAlreadyRegisteredError(WorkflowError):
    code = "workflow_already_registered"
    http_status = 409


class SessionNotFoundError(WorkflowError):
    code = "session_not_found"
    http_status = 404


class RunNotFoundError(WorkflowError):
    code = "run_not_found"
    http_status = 404


class InvalidTransitionError(WorkflowError):
    """An illegal run-status transition was attempted."""

    code = "invalid_transition"
    http_status = 409


class WorkflowExecutionError(WorkflowError):
    """A workflow step failed and could not be recovered by retry."""

    code = "workflow_execution_error"


class StepTimeoutError(WorkflowError):
    """A single execution step exceeded its time budget."""

    code = "workflow_step_timeout"


class RetryExhaustedError(WorkflowError):
    """All retry attempts for a step were exhausted."""

    code = "workflow_retry_exhausted"


class CheckpointError(WorkflowError):
    code = "workflow_checkpoint_error"


class RecoveryError(WorkflowError):
    code = "workflow_recovery_error"
