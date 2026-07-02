"""HTTP control-plane client — talks to the /v1 API over the network.

The target implementation for remote orchestrators: the run reports state and
pulls commands/decisions over authenticated HTTPS, never touching the DB. Uses
only the stdlib (urllib) so it adds no dependency to the orchestrator image.

Contract (see base.ControlPlaneClient): every method is fail-soft — a network
blip logs and returns a benign default rather than raising — except
``fetch_project`` at bootstrap, which surfaces the error.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Optional

from .base import ControlPlaneClient
from .types import Command, DecisionView, ProjectView


def _default_log(msg: str, level: str = "INFO") -> None:
    print(f"[{level}] {msg}")


class HttpControlPlaneClient(ControlPlaneClient):
    def __init__(self, base_url: str, token: str, project_id: str,
                 log_fn=None, timeout: float = 15.0):
        # base_url includes the /v1 prefix, e.g. "https://cp.example.com/v1".
        self._base = (base_url or "").rstrip("/")
        self._token = token or ""
        self._project_id = project_id or ""
        self.log = log_fn or _default_log
        self._timeout = timeout
        self._errors = 0

    # ── internals ────────────────────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        return f"{self._base}/projects/{self._project_id}{path}"

    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 *, fatal: bool = False) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self._url(path), data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
            self._errors = 0
            return json.loads(raw) if raw else None
        except Exception as e:
            if fatal:
                raise
            self._note_error(e)
            return None

    def _note_error(self, e: Exception) -> None:
        self._errors += 1
        if self._errors <= 3:
            self.log(f"control-plane (http) call failed ({self._errors}): {e}", "WARN")

    # ── capability ─────────────────────────────────────────────────────────────────
    @property
    def available(self) -> bool:
        return bool(self._base and self._token and self._project_id)

    @property
    def emits_events(self) -> bool:
        # No shared filesystem over HTTP → the dashboard needs pushed log lines.
        return True

    # ── bootstrap / reads ──────────────────────────────────────────────────────────
    def fetch_project(self) -> Optional[ProjectView]:
        if not self.available:
            return None
        data = self._request("GET", "", fatal=True)
        if not data:
            return None
        return ProjectView(
            id=data.get("id", self._project_id),
            name=data.get("name", "") or "",
            autonomy_level=data.get("autonomy_level", "") or "collaborative",
            status=data.get("status", "") or "",
            phase=data.get("phase", "") or "",
            raw=data,
        )

    def get_autonomy(self) -> Optional[str]:
        if not self.available:
            return None
        data = self._request("GET", "/autonomy")
        return (data or {}).get("level") or None

    # ── status writes ────────────────────────────────────────────────────────────────
    def report_status(self, **fields) -> None:
        if not self.available or not fields:
            return
        self._request("POST", "/status", fields)

    def set_activity(self, text: str) -> None:
        if not self.available:
            return
        self._request("POST", "/activity", {"text": text})

    def set_control_state(self, state: str) -> None:
        if not self.available:
            return
        self._request("POST", "/control-state", {"state": state})

    def set_autonomy(self, level: str) -> None:
        if not self.available:
            return
        self._request("POST", "/autonomy", {"level": level})

    # ── commands (peek + ack) ─────────────────────────────────────────────────────────
    def poll_commands(self) -> list[Command]:
        if not self.available:
            return []
        data = self._request("GET", "/commands")
        rows = (data or {}).get("commands") or []
        return [Command(id=r.get("id", ""), kind=r.get("kind", ""),
                        payload=r.get("payload", ""), source=r.get("source", "webapp"),
                        created_by=r.get("created_by", "")) for r in rows]

    def ack_command(self, cmd_id: str) -> None:
        if not self.available or not cmd_id:
            return
        self._request("POST", f"/commands/{cmd_id}/ack")

    # ── conversation ────────────────────────────────────────────────────────────────────
    def append_message(self, role: str, text: str, kind: str = "message",
                       meta: Optional[dict] = None) -> None:
        if not self.available:
            return
        self._request("POST", "/messages",
                      {"role": role, "text": text, "kind": kind, "meta": meta})

    # ── decisions ─────────────────────────────────────────────────────────────────────────
    def open_decision(self, question: str, options: list[str], *,
                      kind: str = "decision", context: str = "",
                      default_index: int = 0,
                      timeout_action: str = "proceed_default",
                      deadline_at: Optional[datetime] = None) -> Optional[str]:
        if not self.available:
            return None
        body = {
            "question": question, "options": list(options or []),
            "kind": kind, "context": context, "default_index": default_index,
            "timeout_action": timeout_action,
            "deadline_at": deadline_at.isoformat() if deadline_at else None,
        }
        data = self._request("POST", "/decisions", body)
        return (data or {}).get("decision_id")

    def get_decision(self, decision_id: str) -> Optional[DecisionView]:
        if not self.available or not decision_id:
            return None
        data = self._request("GET", f"/decisions/{decision_id}")
        if not data:
            return None
        return DecisionView(
            id=data.get("id", decision_id),
            status=data.get("status", ""),
            answer_index=data.get("answer_index", -1),
            answer_text=data.get("answer_text", "") or "",
            source=data.get("source", "") or "",
        )

    # ── live output ───────────────────────────────────────────────────────────────────────
    def append_events(self, lines: list[dict]) -> None:
        if not self.available or not lines:
            return
        self._request("POST", "/events", {"lines": lines})

    def register_artifact(self, **ref) -> None:
        if not self.available:
            return
        self._request("POST", "/artifacts", dict(ref))

    def put_state(self, name: str, data: dict) -> None:
        if not self.available or not name:
            return
        self._request("PUT", f"/state/{name}", {"data": data or {}})
