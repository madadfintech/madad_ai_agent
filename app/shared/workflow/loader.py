"""Workflow loader — compiles definitions into runnable LangGraph graphs.

Compilation is relatively expensive, so compiled graphs are cached per
(name, version). The cache is safe because compiled graphs are stateless: all
per-run state lives in the checkpointer, keyed by ``thread_id``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .checkpoint import CheckpointerProvider
from .definition import GraphBuilder, WorkflowDefinition
from .registry import WorkflowRegistry
from .state import WorkflowState


@dataclass(frozen=True)
class CompiledWorkflow:
    """A compiled, runnable workflow."""

    definition: WorkflowDefinition
    graph: Any  # langgraph CompiledStateGraph
    state_schema: type[WorkflowState]

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def version(self) -> int:
        return self.definition.version


class WorkflowLoader:
    """Builds and caches compiled graphs from registered definitions."""

    def __init__(
        self,
        registry: WorkflowRegistry,
        checkpointer_provider: CheckpointerProvider,
    ) -> None:
        self._registry = registry
        self._checkpointer = checkpointer_provider
        self._cache: dict[tuple[str, int], CompiledWorkflow] = {}

    def load(self, name: str, version: int | None = None) -> CompiledWorkflow:
        definition = self._registry.get(name, version)
        key = (definition.name, definition.version)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        builder = GraphBuilder(definition.state_schema)
        definition.build(builder)
        graph = builder.compile(self._checkpointer.get())

        compiled = CompiledWorkflow(
            definition=definition,
            graph=graph,
            state_schema=definition.state_schema,
        )
        self._cache[key] = compiled
        return compiled

    def invalidate(self, name: str, version: int | None = None) -> None:
        if version is None:
            for key in [k for k in self._cache if k[0] == name]:
                self._cache.pop(key, None)
        else:
            self._cache.pop((name, version), None)
