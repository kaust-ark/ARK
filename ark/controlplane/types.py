"""Plain data views crossing the control-plane boundary.

These are transport-agnostic dataclasses so orchestrator code never holds ORM
objects (or any ``website.dashboard.db`` type) past a call. The HTTP client
(Phase 1, step 4) deserializes JSON into the same shapes the LocalDb client
builds from SQLModel rows, so callers are identical regardless of transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProjectView:
    """The subset of a project record the orchestrator reads at bootstrap / runtime.

    ``raw`` carries the full record dict for forward-compatibility so new fields
    are readable without a schema bump here.
    """

    id: str
    name: str = ""
    autonomy_level: str = "collaborative"
    status: str = ""
    phase: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class Command:
    """A control command enqueued by the webapp/Telegram for a running orchestrator.

    Kinds: ``pause`` | ``resume`` | ``stop`` | ``steer`` | ``set_autonomy``.
    ``payload`` is the steer text / autonomy value (empty for pause/resume/stop).
    """

    id: str
    kind: str
    payload: str = ""
    source: str = "webapp"
    created_by: str = ""


@dataclass
class DecisionView:
    """The state of a pending decision, polled by the orchestrator until resolved.

    ``status``: ``pending`` | ``answered`` | ``timed_out`` | ``cancelled``.
    """

    id: str
    status: str
    answer_index: int = -1
    answer_text: str = ""
    source: str = ""
