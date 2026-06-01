"""Synthetic workflows used to exercise the runtime.

These are deliberately trivial test fixtures — NOT business workflows. They only
exist to drive the runtime's behaviour (completion, interrupt/resume, retry,
timeout) in unit tests.
"""

from __future__ import annotations

import asyncio

from app.shared.workflow import (
    GraphBuilder,
    HistoryEntry,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowState,
    await_input,
)


class GreetState(WorkflowState):
    """Typed state for the synthetic workflows."""

    name: str = ""
    greeted: bool = False
    finished: bool = False


class LinearWorkflow(WorkflowDefinition):
    """Two sequential nodes, no interrupts — runs straight to completion."""

    name = "test_linear"
    version = 1
    state_schema = GreetState

    def build(self, graph: GraphBuilder) -> None:
        async def greet(state: GreetState, ctx: WorkflowContext) -> dict:
            return {
                "greeted": True,
                "history": [HistoryEntry(step="greet", at=ctx.clock.now().isoformat())],
            }

        async def finish(state: GreetState, ctx: WorkflowContext) -> dict:
            return {"finished": True, "data": {"summary": "done"}}

        graph.add_node("greet", greet)
        graph.add_node("finish", finish)
        graph.set_entry("greet")
        graph.add_edge("greet", "finish")
        graph.set_finish("finish")


class AskNameWorkflow(WorkflowDefinition):
    """Pauses for input, then completes — exercises interrupt/resume."""

    name = "test_ask_name"
    version = 1
    state_schema = GreetState

    def build(self, graph: GraphBuilder) -> None:
        async def ask(state: GreetState, ctx: WorkflowContext) -> dict:
            answer = await_input({"prompt": "What is your name?"})
            return {
                "name": answer,
                "history": [HistoryEntry(step="ask", at=ctx.clock.now().isoformat())],
            }

        async def finish(state: GreetState, ctx: WorkflowContext) -> dict:
            return {"finished": True, "data": {"greeting": f"Hello {state.name}"}}

        graph.add_node("ask", ask)
        graph.add_node("finish", finish)
        graph.set_entry("ask")
        graph.add_edge("ask", "finish")
        graph.set_finish("finish")


class FlakyWorkflow(WorkflowDefinition):
    """A node that fails ``fail_times`` before succeeding — exercises retry.

    The call counter lives on the instance so the test can assert how many times
    the node ran across retries.
    """

    name = "test_flaky"
    version = 1
    state_schema = GreetState

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def build(self, graph: GraphBuilder) -> None:
        async def work(state: GreetState, ctx: WorkflowContext) -> dict:
            self.calls += 1
            if self.calls <= self.fail_times:
                raise RuntimeError(f"transient failure #{self.calls}")
            return {"finished": True}

        graph.add_node("work", work)
        graph.set_entry("work")
        graph.set_finish("work")


class SlowWorkflow(WorkflowDefinition):
    """A node that sleeps longer than the step budget — exercises timeout."""

    name = "test_slow"
    version = 1
    state_schema = GreetState

    def __init__(self, sleep_for: float = 1.0) -> None:
        self.sleep_for = sleep_for

    def build(self, graph: GraphBuilder) -> None:
        async def slow(state: GreetState, ctx: WorkflowContext) -> dict:
            await asyncio.sleep(self.sleep_for)
            return {"finished": True}

        graph.add_node("slow", slow)
        graph.set_entry("slow")
        graph.set_finish("slow")
