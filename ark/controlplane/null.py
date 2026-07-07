"""No-op control-plane client.

Used when the orchestrator has no control plane at all — e.g. a Telegram-only or
fully headless run (the old ``self._db_path is None`` case). Every method is a
safe no-op; ``available`` is False so callers skip control-plane work entirely.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from .base import ControlPlaneClient
from .types import Command, DecisionView, ProjectView


class NullControlPlaneClient(ControlPlaneClient):
    @property
    def available(self) -> bool:
        return False

    def fetch_project(self) -> Optional[ProjectView]:
        return None

    def get_autonomy(self) -> Optional[str]:
        return None

    def report_status(self, **fields) -> None:
        pass

    def set_activity(self, text: str) -> None:
        pass

    def set_control_state(self, state: str) -> None:
        pass

    def set_autonomy(self, level: str) -> None:
        pass

    def poll_commands(self) -> list[Command]:
        return []

    def ack_command(self, cmd_id: str) -> None:
        pass

    def append_message(self, role: str, text: str, kind: str = "message",
                       meta: Optional[dict] = None) -> None:
        pass

    def open_decision(self, question: str, options: list[str], *,
                      kind: str = "decision", context: str = "",
                      default_index: int = 0,
                      timeout_action: str = "proceed_default",
                      deadline_at: Optional[datetime] = None) -> Optional[str]:
        return None

    def get_decision(self, decision_id: str) -> Optional[DecisionView]:
        return None

    def append_events(self, lines: list[dict]) -> None:
        pass

    def register_artifact(self, **ref) -> None:
        pass

    def upload_artifact(self, key: str, data: bytes, *, kind: str = "",
                        content_type: str = "") -> None:
        pass

    def put_state(self, name: str, data: dict) -> None:
        pass
