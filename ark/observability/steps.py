"""Parse OpenHands JSONL events into a clean, redacted step log.

The OpenHands ``--json`` stream interleaves real events with terminal-UI noise.
:func:`parse_line` keeps only the meaningful events and maps each to a
:class:`StepEvent`. :class:`StepLogger` writes them to ``agent_steps.jsonl`` and
emits one human-readable line per step (respecting a verbosity level), routing
everything through a :class:`Redactor` first so a secret never lands in a log.

The event-kind mapping is best-effort: OpenHands' action schema varies by
model/version, so unknown shapes degrade to a generic ``action`` step rather
than being dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ark.intervention.policy import redact_secrets


# verbosity → the step types that produce a human-readable line
_VERBOSITY_TYPES = {
    "quiet":   {"finish", "error"},
    "normal":  {"command", "edit", "finish", "error", "thought"},
    "verbose": {"command", "edit", "read", "result", "finish", "error", "thought"},
    "debug":   {"command", "edit", "read", "result", "finish", "error", "thought", "action", "raw"},
}

_TYPE_ICON = {
    "command": "$", "edit": "✎", "read": "▸", "result": "→",
    "thought": "💭", "finish": "✓", "error": "✗", "action": "•",
}

_MAX_SUMMARY = 200


@dataclass
class StepEvent:
    type: str                      # command/edit/read/result/thought/finish/error/action
    summary: str                   # short, redacted, one line
    detail: str = ""               # longer redacted payload (for jsonl / verbose)
    raw_kind: str = ""             # original OpenHands kind, for debugging


def _truncate(s: str, n: int = _MAX_SUMMARY) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _first_str(d: dict, *keys) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def parse_line(line: str) -> Optional[StepEvent]:
    """Map one JSONL line to a StepEvent, or None for noise / non-events."""
    line = (line or "").strip()
    if not (line.startswith("{") and line.endswith("}")):
        return None
    try:
        evt = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    kind = evt.get("kind")

    if kind == "ActionEvent":
        return _parse_action(evt)
    if kind == "ObservationEvent":
        obs = evt.get("observation") or evt.get("content") or evt
        text = _first_str(obs, "content", "output", "stdout", "text") if isinstance(obs, dict) else str(obs)
        return StepEvent("result", _truncate(text), detail=text, raw_kind=kind)
    if kind == "MessageEvent" and evt.get("source") == "agent":
        msg = evt.get("llm_message") or {}
        parts = msg.get("content") or []
        text = "".join(p.get("text", "") for p in parts
                       if isinstance(p, dict) and p.get("type") == "text")
        if not text.strip():
            return None
        return StepEvent("thought", _truncate(text), detail=text, raw_kind=kind)
    if kind == "ConversationErrorEvent":
        detail = str(evt.get("detail") or evt.get("code") or "")
        return StepEvent("error", _truncate(detail) or "error", detail=detail, raw_kind=kind)
    # OpenHands terminal/file observations arrive as source=="environment" with
    # no `kind`. Best-effort: surface the output text as a result step.
    if evt.get("source") == "environment":
        text = _first_str(evt, "content", "output", "stdout", "text", "observation", "result")
        if not text:
            obs = evt.get("observation")
            if isinstance(obs, dict):
                text = _first_str(obs, "content", "output", "stdout", "text")
        if text:
            return StepEvent("result", _truncate(text), detail=text,
                             raw_kind=evt.get("tool_name") or "observation")
        return None
    return None


def _parse_action(evt: dict) -> Optional[StepEvent]:
    action = evt.get("action") or {}
    if not isinstance(action, dict):
        return None
    akind = action.get("kind") or ""

    # bash / shell command
    cmd = _first_str(action, "command", "cmd")
    if cmd or "Bash" in akind or "Execute" in akind or "Cmd" in akind:
        cmd = cmd or _first_str(action, "code")
        # No "$ " prefix here — the logger renders the "$" icon for commands.
        return StepEvent("command", _truncate(cmd), detail=cmd, raw_kind=akind)

    # file edit / write
    if any(t in akind for t in ("Edit", "Write", "StrReplace", "Create")):
        path = _first_str(action, "path", "file_path", "filename")
        return StepEvent("edit", f"edit {path}".strip(), detail=path, raw_kind=akind)

    # file read
    if any(t in akind for t in ("Read", "View", "Cat")):
        path = _first_str(action, "path", "file_path", "filename")
        return StepEvent("read", f"read {path}".strip(), detail=path, raw_kind=akind)

    # finish
    if "Finish" in akind:
        msg = _first_str(action, "message", "thought")
        return StepEvent("finish", _truncate(msg) or "finished", detail=msg, raw_kind=akind)

    # generic / unknown action
    thought = _first_str(action, "thought")
    return StepEvent("action", _truncate(thought) or (akind or "action"),
                     detail=thought, raw_kind=akind)


class Redactor:
    """Scrub secret values before they reach a log line or the step journal."""

    def __init__(self, secret_values: Optional[list] = None):
        # literal secret strings (API keys, tokens) registered by the caller
        self._values = sorted({v for v in (secret_values or []) if v and len(v) >= 6},
                              key=len, reverse=True)

    def add(self, value: str) -> None:
        if value and len(value) >= 6 and value not in self._values:
            self._values.append(value)
            self._values.sort(key=len, reverse=True)

    def redact(self, text: str) -> str:
        if not text:
            return text
        out = redact_secrets(text)          # NAME=value patterns
        for v in self._values:              # known literal secrets
            if v in out:
                out = out.replace(v, "***")
        return out


@dataclass
class StepLogger:
    """Write the step journal + emit human-readable step lines."""
    steps_path: Optional[Path] = None
    log_step_fn: Optional[Callable] = None     # (message:str, status:str) -> None
    redactor: Redactor = field(default_factory=Redactor)
    verbosity: str = "normal"
    agent_type: str = ""
    _count: int = 0

    def feed_line(self, line: str) -> Optional[StepEvent]:
        evt = parse_line(line)
        if evt is None:
            return None
        self.emit(evt)
        return evt

    def emit(self, evt: StepEvent) -> None:
        self._count += 1
        summary = self.redactor.redact(evt.summary)
        detail = self.redactor.redact(evt.detail)
        self._write_jsonl(evt, summary, detail)
        if self._should_show(evt.type):
            icon = _TYPE_ICON.get(evt.type, "•")
            status = "error" if evt.type == "error" else (
                "success" if evt.type == "finish" else "progress")
            if self.log_step_fn:
                self.log_step_fn(f"{icon} {summary}", status)

    def _should_show(self, step_type: str) -> bool:
        return step_type in _VERBOSITY_TYPES.get(self.verbosity, _VERBOSITY_TYPES["normal"])

    def _write_jsonl(self, evt: StepEvent, summary: str, detail: str) -> None:
        if not self.steps_path:
            return
        try:
            from datetime import datetime
            self.steps_path.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "n": self._count,
                "ts": datetime.now().isoformat(),
                "agent": self.agent_type,
                "type": evt.type,
                "summary": summary,
                "detail": detail[:2000],
                "raw_kind": evt.raw_kind,
            }
            with self.steps_path.open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
