"""Workflow definitions and the graph builder.

A :class:`WorkflowDefinition` declares a workflow's name, version, state schema,
and how its graph is wired. :class:`GraphBuilder` is a thin, intention-revealing
wrapper over LangGraph's ``StateGraph`` so definitions don't touch LangGraph
internals directly and so every node is consistently context-injected.

This module provides the *abstractions only* — no concrete business workflows.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from .nodes import BaseNode, NodeFn, as_graph_node, coerce_node
from .state import WorkflowState

# Condition function for conditional edges: (state) -> routing key.
Router = Callable[[Any], str | Awaitable[str]]


class GraphBuilder:
    """Fluent wrapper over ``StateGraph`` that context-injects every node."""

    START = START
    END = END

    def __init__(self, state_schema: type[WorkflowState]) -> None:
        self._sg: StateGraph[Any, Any, Any, Any] = StateGraph(state_schema)

    def add_node(
        self,
        name: str,
        node: BaseNode | NodeFn,
        *,
        retry_policy: Any | None = None,
        timeout: float | None = None,
    ) -> GraphBuilder:
        """Register a node. Accepts a :class:`BaseNode` or an async ``(state, ctx)``
        function. ``retry_policy``/``timeout`` are optional LangGraph-native
        node-level controls (run-level retry/timeout is applied by the executor)."""

        runner = as_graph_node(coerce_node(name, node))
        # LangGraph's add_node overloads bind the node's input type to a concrete
        # schema; our context-injecting wrapper is intentionally Any-typed, so we
        # cast to satisfy the overload resolver.
        self._sg.add_node(
            name, cast(Any, runner), retry_policy=retry_policy, timeout=timeout
        )
        return self

    def add_edge(self, src: str, dst: str) -> GraphBuilder:
        self._sg.add_edge(src, dst)
        return self

    def add_conditional_edges(
        self,
        src: str,
        router: Router,
        path_map: dict[str, str] | None = None,
    ) -> GraphBuilder:
        if path_map is not None:
            self._sg.add_conditional_edges(src, cast(Any, router), cast(Any, path_map))
        else:
            self._sg.add_conditional_edges(src, cast(Any, router))
        return self

    def set_entry(self, name: str) -> GraphBuilder:
        self._sg.add_edge(START, name)
        return self

    def set_finish(self, name: str) -> GraphBuilder:
        self._sg.add_edge(name, END)
        return self

    def compile(self, checkpointer: Any) -> Any:
        return self._sg.compile(checkpointer=checkpointer)


class WorkflowDefinition(ABC):
    """Declarative definition of a workflow.

    Subclasses set the class attributes and implement :meth:`build`.
    """

    name: str
    version: int = 1
    state_schema: type[WorkflowState] = WorkflowState

    @abstractmethod
    def build(self, graph: GraphBuilder) -> None:
        """Wire the workflow's nodes and edges onto ``graph``."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Validate eagerly so misconfiguration fails at import, not at runtime.
        if getattr(cls, "name", None) and not isinstance(cls.version, int):
            raise TypeError(f"{cls.__name__}.version must be an int")
