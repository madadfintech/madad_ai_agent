"""MADAD shared conversational workflow runtime.

The core, reusable orchestration layer every service builds on. Wraps LangGraph
v1.0 with the operational concerns a production conversational platform needs:
deterministic graphs, persistent channel-identity sessions, pause/resume via
interrupts, crash + reconnect recovery, retry, timeout, audit, and events.

Typical use::

    from app.shared.workflow import build_runtime, WorkflowDefinition, Channel

    runtime = build_runtime()
    runtime.register(MyWorkflow())
    result = await runtime.start("my_workflow", Channel.WHATSAPP, "+97455...")
    if result.waiting:
        ...  # ask result.prompt; later: await runtime.resume(channel, id, message=...)
"""

from __future__ import annotations

from .audit import AuditLogger
from .checkpoint import CheckpointerProvider, InMemoryCheckpointerProvider
from .context import Clock, SystemClock, WorkflowContext, current_context, use_context
from .definition import GraphBuilder, WorkflowDefinition
from .enums import (
    RECOVERABLE_STATUSES,
    TERMINAL_STATUSES,
    Channel,
    RunStatus,
    SessionStatus,
    WorkflowEventType,
)
from .errors import (
    CheckpointError,
    InvalidTransitionError,
    RecoveryError,
    RetryExhaustedError,
    RunNotFoundError,
    SessionNotFoundError,
    StepTimeoutError,
    WorkflowAlreadyRegisteredError,
    WorkflowError,
    WorkflowExecutionError,
    WorkflowNotFoundError,
)
from .events import EventBus, InMemoryEventBus, WorkflowEvent
from .executor import ExecutionResult, WorkflowExecutor
from .loader import CompiledWorkflow, WorkflowLoader
from .nodes import BaseNode, FunctionNode, NodeResult, await_input
from .persistence import (
    AuditEntry,
    InMemoryWorkflowRunStore,
    WorkflowRun,
    WorkflowRunStore,
)
from .recovery import RecoveryEngine
from .registry import WorkflowRegistry
from .retry import RetryEngine, RetryPolicy
from .runtime import WorkflowRuntime, build_runtime
from .session import (
    InMemorySessionStore,
    Session,
    SessionManager,
    SessionStore,
)
from .state import HistoryEntry, WorkflowState
from .timeout import TimeoutEngine
from .transitions import TransitionManager
from .utils import derive_session_id, derive_thread_id, new_id, utcnow

__all__ = [
    # runtime facade
    "WorkflowRuntime",
    "build_runtime",
    # enums
    "Channel",
    "RunStatus",
    "SessionStatus",
    "WorkflowEventType",
    "TERMINAL_STATUSES",
    "RECOVERABLE_STATUSES",
    # state + context
    "WorkflowState",
    "HistoryEntry",
    "WorkflowContext",
    "Clock",
    "SystemClock",
    "current_context",
    "use_context",
    # definition + nodes
    "WorkflowDefinition",
    "GraphBuilder",
    "BaseNode",
    "FunctionNode",
    "NodeResult",
    "await_input",
    # registry + loader
    "WorkflowRegistry",
    "WorkflowLoader",
    "CompiledWorkflow",
    # executor
    "WorkflowExecutor",
    "ExecutionResult",
    # sessions
    "Session",
    "SessionManager",
    "SessionStore",
    "InMemorySessionStore",
    # persistence
    "WorkflowRun",
    "AuditEntry",
    "WorkflowRunStore",
    "InMemoryWorkflowRunStore",
    # events + audit
    "WorkflowEvent",
    "EventBus",
    "InMemoryEventBus",
    "AuditLogger",
    # reliability
    "RetryEngine",
    "RetryPolicy",
    "TimeoutEngine",
    "TransitionManager",
    "RecoveryEngine",
    # checkpointing
    "CheckpointerProvider",
    "InMemoryCheckpointerProvider",
    # utils
    "derive_session_id",
    "derive_thread_id",
    "new_id",
    "utcnow",
    # errors
    "WorkflowError",
    "WorkflowNotFoundError",
    "WorkflowAlreadyRegisteredError",
    "SessionNotFoundError",
    "RunNotFoundError",
    "InvalidTransitionError",
    "WorkflowExecutionError",
    "StepTimeoutError",
    "RetryExhaustedError",
    "CheckpointError",
    "RecoveryError",
]
