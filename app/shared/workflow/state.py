"""Base workflow state schema.

This is the LangGraph state schema base class. Business workflows subclass it to
add their own typed fields (and reducers). The runtime only relies on the common
conversational fields defined here.

IMPORTANT: state holds *orchestration* data only — never SME business records.
Business data is owned by Madad and read/written through their APIs.
"""

from __future__ import annotations

from operator import add
from typing import Annotated, Any

from pydantic import BaseModel, Field

from .enums import Channel


class HistoryEntry(BaseModel):
    """A single step in the run's execution trail (used for replay/audit)."""

    step: str
    at: str  # ISO-8601 timestamp
    detail: dict[str, Any] = Field(default_factory=dict)


class WorkflowState(BaseModel):
    """Base conversational workflow state.

    The ``history`` channel uses an additive reducer so each node appends its
    trail without clobbering prior entries. ``data`` is last-write-wins scratch
    space for the workflow's own use.
    """

    # Identity / routing (set by the executor when the run is created).
    session_id: str = ""
    channel: Channel | None = None
    identity: str = ""
    workflow: str = ""

    # Conversational I/O.
    last_input: Any = None

    # Accumulating execution trail (reducer = list append).
    history: Annotated[list[HistoryEntry], add] = Field(default_factory=list)

    # Free-form scratch space for the workflow (last-write-wins).
    data: dict[str, Any] = Field(default_factory=dict)
