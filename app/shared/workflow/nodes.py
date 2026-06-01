"""Shared workflow node abstractions.

Nodes are the unit of work in a graph. They are deliberately thin and
deterministic: a node receives the current state and a :class:`WorkflowContext`
(its only door to the outside world) and returns a dict of state updates.

To pause a conversation and wait for the user's next message, a node calls
:func:`await_input`, which wraps LangGraph's ``interrupt``. The executor
checkpoints at that point; the next inbound message resumes the graph with the
provided value.

NO business logic lives here — only the abstractions business workflows build on.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.types import interrupt

from .context import WorkflowContext, current_context
from .state import WorkflowState

NodeResult = dict[str, Any]
# Params are intentionally unconstrained: business workflows pass node functions
# whose first parameter is their own typed WorkflowState subclass.
NodeFn = Callable[..., Awaitable[NodeResult | None]]


class BaseNode(ABC):
    """Base class for workflow nodes.

    Subclasses set :attr:`name` and implement :meth:`run`. The runtime injects the
    context; nodes must not construct clients or read globals.
    """

    name: str

    def __init__(self, name: str | None = None) -> None:
        if name is not None:
            self.name = name
        if not getattr(self, "name", None):
            raise ValueError("A node must have a non-empty 'name'.")

    @abstractmethod
    async def run(self, state: WorkflowState, ctx: WorkflowContext) -> NodeResult | None:
        """Execute the node and return state updates (or None for no change)."""


class FunctionNode(BaseNode):
    """Adapt a plain async ``(state, ctx) -> dict`` function into a node."""

    def __init__(self, name: str, fn: NodeFn) -> None:
        super().__init__(name)
        self._fn = fn

    async def run(self, state: WorkflowState, ctx: WorkflowContext) -> NodeResult | None:
        return await self._fn(state, ctx)


def coerce_node(name: str, node: BaseNode | NodeFn) -> BaseNode:
    """Return a :class:`BaseNode` for either a node instance or a function."""

    if isinstance(node, BaseNode):
        return node
    return FunctionNode(name, node)


def as_graph_node(node: BaseNode) -> Callable[[Any], Awaitable[NodeResult]]:
    """Wrap a node into a LangGraph-compatible async callable.

    The wrapper pulls the active :class:`WorkflowContext` from the contextvar the
    executor sets around each invocation, so nodes receive their dependencies
    without LangGraph needing to know about them.

    NOTE: ``state`` is intentionally annotated ``Any``. LangGraph reads a node's
    first-parameter annotation as the node's *input schema*; annotating it with a
    concrete model would coerce the state to that type and silently drop the
    workflow's subclass fields after a checkpoint round-trip. ``Any`` lets the
    graph's own state schema flow through unchanged.
    """

    async def _runner(state: Any) -> NodeResult:
        ctx = current_context()
        result = await node.run(state, ctx)
        return result or {}

    _runner.__name__ = node.name
    return _runner


def await_input(payload: dict[str, Any] | str) -> Any:
    """Pause the workflow and wait for the next inbound message.

    Returns the value supplied when the run is resumed (the user's reply). Must be
    called from inside a node during graph execution.
    """

    return interrupt(payload)
