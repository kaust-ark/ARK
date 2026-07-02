"""The single narrow contract between the orchestrator and the control plane.

Every crossing that used to be a direct ``website.dashboard.db`` call in the
orchestrator now goes through this interface (see CONTROL_PLANE_BOUNDARY.md).
Three implementations back it:

* ``LocalDbControlPlaneClient`` — wraps the existing ``db.py`` helpers in-process
  (single-node dev, SLURM-on-shared-DB). The ONLY remaining importer of
  ``website.dashboard.db`` — deleted when remote hosting is the only path.
* ``NullControlPlaneClient`` — no-op (Telegram-only or fully headless runs).
* ``HttpControlPlaneClient`` — talks to the ``/v1`` API (Phase 1, step 4).

**Contract rules**
* Every method is *fail-soft*: log and swallow, never raise — mirroring the old
  ``_sync_db`` behavior. The one exception is ``fetch_project`` at bootstrap,
  which callers may treat as fatal.
* ``available`` gates work the way ``self._db_path and self._project_id`` used to.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from .types import Command, DecisionView, ProjectView


class ControlPlaneClient(ABC):
    """Abstract boundary. See module docstring and CONTROL_PLANE_BOUNDARY.md."""

    # ── Capability ────────────────────────────────────────────────────────────
    @property
    @abstractmethod
    def available(self) -> bool:
        """True when a real control plane is reachable (replaces the old
        ``self._db_path and self._project_id`` guards)."""

    @property
    def emits_events(self) -> bool:
        """Whether ``append_events`` does real work — True only for transports
        without a shared filesystem (HTTP). Lets the orchestrator skip event
        buffering when the dashboard reads logs off the shared FS (LocalDb)."""
        return False

    # ── Bootstrap / config (reads) ──────────────────────────────────────────────
    @abstractmethod
    def fetch_project(self) -> Optional[ProjectView]:
        """Load the project record the orchestrator starts from. May be treated
        as fatal by the caller (unlike the fail-soft writers)."""

    @abstractmethod
    def get_autonomy(self) -> Optional[str]:
        """Current autonomy level, or None if unavailable (caller falls back to
        config/default). Called frequently, so kept separate from fetch_project."""

    # ── Status / progress (writes) ──────────────────────────────────────────────
    @abstractmethod
    def report_status(self, **fields) -> None:
        """Partial, idempotent update of runtime fields (status, phase, iteration,
        score, score_history, cost, pid, error_message, language, …)."""

    @abstractmethod
    def set_activity(self, text: str) -> None:
        """One-line live activity string for the dashboard."""

    @abstractmethod
    def set_control_state(self, state: str) -> None:
        """UI run state: '' (running) | 'paused' | 'awaiting'."""

    @abstractmethod
    def set_autonomy(self, level: str) -> None:
        """Persist an autonomy change echoed back from a set_autonomy command."""

    # ── Commands (pull) ─────────────────────────────────────────────────────────
    @abstractmethod
    def poll_commands(self) -> list[Command]:
        """Return pending control commands. LocalDb consumes on read (legacy
        semantics) and treats ``ack_command`` as a no-op; the HTTP client keeps
        commands pending until ``ack_command`` (at-least-once delivery, D2)."""

    @abstractmethod
    def ack_command(self, cmd_id: str) -> None:
        """Acknowledge a command as applied. No-op for LocalDb."""

    # ── Conversation thread (write) ─────────────────────────────────────────────
    @abstractmethod
    def append_message(self, role: str, text: str, kind: str = "message",
                       meta: Optional[dict] = None) -> None:
        """Append a bubble to the project's chat thread."""

    # ── Decisions ───────────────────────────────────────────────────────────────
    @abstractmethod
    def open_decision(self, question: str, options: list[str], *,
                      kind: str = "decision", context: str = "",
                      default_index: int = 0,
                      timeout_action: str = "proceed_default",
                      deadline_at: Optional[datetime] = None) -> Optional[str]:
        """Open a decision for the human; returns its id (or None if unavailable).
        Cancels any prior open decision for the project."""

    @abstractmethod
    def get_decision(self, decision_id: str) -> Optional[DecisionView]:
        """Poll a decision's current state.

        Note: the orchestrator only *opens* and *polls* decisions (D1). Notifying
        the human, capturing the answer from any channel, and enforcing the
        timeout are owned by the control-plane HITL engine
        (``website.dashboard.hitl``), not the orchestrator."""

    # ── Live output (new capability; removes shared-FS log/artifact reads) ──────
    @abstractmethod
    def append_events(self, lines: list[dict]) -> None:
        """Stream agent-step / log lines for live display. No-op for LocalDb
        (the dashboard reads the on-disk JSONL directly in single-node mode)."""

    @abstractmethod
    def register_artifact(self, **ref) -> None:
        """Register an artifact reference (PDF/figure). The bytes are written to
        the artifact store; this records the reference (Phase 3, ADR-0012)."""

    @abstractmethod
    def put_state(self, name: str, data: dict) -> None:
        """Project a state document (paper_state, action_plan, findings, memory,
        dev_phase_state) to the control plane for dashboard / export-ZIP
        consumption. Best-effort; the orchestrator's local YAML stays
        authoritative (Phase 3, ADR-0013)."""
