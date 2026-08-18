"""Parse agent-runtime JSONL events into a clean, redacted step log.

Two runtime event dialects are understood:

* **OpenHands** ``--json`` stream events (``kind``-keyed: ``ActionEvent``,
  ``ObservationEvent``, ``MessageEvent``, ``ConversationErrorEvent``), which
  arrive interleaved with terminal-UI noise on stdout.
* **DeepSeek Harness (dsh)** session-log events (``type``-keyed:
  ``tool/call``, ``tool/result``, ``assistant/message``, ``turn/end``), which
  the dsh engine tails from the session's append-only ``session.jsonl``.

:func:`parse_line` keeps only the meaningful events and maps each to a
:class:`StepEvent`. :class:`StepLogger` writes them to ``agent_steps.jsonl`` and
emits one human-readable line per step (respecting a verbosity level), routing
everything through a :class:`Redactor` first so a secret never lands in a log.

The event mapping is best-effort: runtime schemas vary by model/version, so
unknown shapes degrade to a generic ``action`` step rather than being dropped.
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
    "command": "$", "edit": "✎", "read": "▸", "result": "↳",
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
    # dsh session-log events are keyed by a slash-namespaced `type`
    # ("tool/call", "turn/end", …); OpenHands events never use that shape.
    if kind is None and isinstance(evt.get("type"), str) and "/" in evt["type"]:
        return _parse_dsh(evt)

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


# dsh tool names → step types (see `docs/DEEPSEEK_HARNESS.md`). The bash tool's
# argument is `command`; fs tools take `file_path`; str_replace_editor takes
# a sub-verb `command` (view/create/str_replace/insert) plus `path`.
_DSH_EDIT_TOOLS = {"write", "edit"}
_DSH_READ_TOOLS = {"read", "read_image"}


def _parse_dsh(evt: dict) -> Optional[StepEvent]:
    """Map one dsh session-log event (``type``-keyed) to a StepEvent."""
    etype = evt.get("type") or ""
    data = evt.get("data") or {}
    if not isinstance(data, dict):
        return None

    if etype == "tool/call":
        name = str(data.get("name") or "")
        args = data.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        if name == "bash":
            cmd = _first_str(args, "command")
            return StepEvent("command", _truncate(cmd), detail=cmd, raw_kind="dsh:bash")
        if name in _DSH_EDIT_TOOLS:
            path = _first_str(args, "file_path", "path")
            return StepEvent("edit", (f"edit {path}").strip() if path else "edit",
                             detail=path, raw_kind=f"dsh:{name}")
        if name in _DSH_READ_TOOLS:
            path = _first_str(args, "file_path", "path")
            return StepEvent("read", (f"read {path}").strip() if path else "read",
                             detail=path, raw_kind=f"dsh:{name}")
        if name == "str_replace_editor":
            verb = _first_str(args, "command")
            path = _first_str(args, "path")
            if verb in _VIEW_VERBS:
                return StepEvent("read", (f"read {path}").strip() if path else "read",
                                 detail=path, raw_kind="dsh:str_replace_editor")
            return StepEvent("edit", (f"edit {path}").strip() if path else "edit",
                             detail=path, raw_kind="dsh:str_replace_editor")
        summary = name or "tool"
        return StepEvent("action", _truncate(summary),
                         detail=_truncate(str(args), 500), raw_kind=f"dsh:{name}")

    if etype == "tool/result":
        msg = data.get("message") or {}
        parts = msg.get("content") if isinstance(msg, dict) else None
        text = ""
        if isinstance(parts, list):
            text = "".join(p.get("text", "") for p in parts
                           if isinstance(p, dict) and p.get("type") == "text")
        if not text.strip():
            return None
        return StepEvent("result", _truncate(text), detail=text, raw_kind="dsh:tool/result")

    if etype == "assistant/message":
        msg = data.get("message") or {}
        parts = msg.get("content") if isinstance(msg, dict) else None
        text = ""
        if isinstance(parts, list):
            text = "".join(p.get("text", "") for p in parts
                           if isinstance(p, dict) and p.get("type") == "text")
        if not text.strip():
            return None
        return StepEvent("thought", _truncate(text), detail=text, raw_kind="dsh:assistant")

    if etype == "turn/end":
        reason = data.get("reason") or {}
        rkind = reason.get("kind") if isinstance(reason, dict) else None
        if rkind == "error":
            err = reason.get("error") or {}
            detail = f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
            return StepEvent("error", _truncate(detail) or "error",
                             detail=detail, raw_kind="dsh:turn/end")
        if rkind == "completed":
            return StepEvent("finish", "finished", raw_kind="dsh:turn/end")
        if rkind:
            return StepEvent("error", f"turn ended: {rkind}",
                             detail=str(rkind), raw_kind="dsh:turn/end")
        return None

    return None


# OpenHands' file-editor tool passes its sub-verb in the `command` field — these
# are NOT bash commands.
_EDITOR_VERBS = {"create", "str_replace", "insert", "undo_edit", "append"}
_VIEW_VERBS = {"view", "open"}
_META_VERBS = {"plan", "think"}        # task-tracker / planning tools, not bash


def _parse_action(evt: dict) -> Optional[StepEvent]:
    action = evt.get("action") or {}
    if not isinstance(action, dict):
        return None
    akind = action.get("kind") or ""
    tool = (evt.get("tool_name") or action.get("tool_name") or "").lower()
    cmd = _first_str(action, "command", "cmd")
    path = _first_str(action, "path", "file_path", "filename")
    verb = cmd.strip().lower() if cmd else ""    # single-word only matches a tool sub-verb

    # File ops (editor): a `path` is present, or the verb/kind says so. The
    # editor's `command` is view/create/str_replace/... — never bash. Using
    # path-presence as the discriminator keeps real bash (`cat x`, no path) out.
    is_editor = (bool(path) or verb in _EDITOR_VERBS or verb in _VIEW_VERBS
                 or any(t in akind for t in ("Edit", "Write", "StrReplace", "Create", "FileEditor", "View", "Read"))
                 or "editor" in tool)
    if is_editor:
        if verb in _VIEW_VERBS or any(t in akind for t in ("Read", "View")):
            return StepEvent("read", (f"read {path}").strip() if path else "read",
                             detail=path, raw_kind=akind or verb)
        label = (f"edit {path}").strip() if path else (f"edit ({verb})" if verb else "edit")
        return StepEvent("edit", label, detail=path, raw_kind=akind or verb)

    # Planner / task-tracker meta tools (not bash)
    if verb in _META_VERBS or "task" in akind.lower() or "tracker" in tool:
        return StepEvent("action", verb or "plan", detail=cmd, raw_kind=akind or verb)

    # Finish
    if "Finish" in akind or tool == "finish":
        msg = _first_str(action, "message", "thought")
        return StepEvent("finish", _truncate(msg) or "finished", detail=msg, raw_kind=akind)

    # Real bash command
    if cmd or "Bash" in akind or "Execute" in akind or "Cmd" in akind or tool in ("bash", "terminal", "execute_bash"):
        cmd = cmd or _first_str(action, "code")
        return StepEvent("command", _truncate(cmd), detail=cmd, raw_kind=akind)

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
    base_dir: str = ""                          # stripped from paths for readability
    _count: int = 0

    def feed_line(self, line: str) -> Optional[StepEvent]:
        evt = parse_line(line)
        if evt is None:
            return None
        self.emit(evt)
        return evt

    def _shorten(self, text: str) -> str:
        """Strip the long project-dir prefix so lines read relative to the project."""
        if self.base_dir and text:
            text = text.replace(self.base_dir + "/", "").replace(self.base_dir, ".")
        return text

    def emit(self, evt: StepEvent) -> None:
        self._count += 1
        summary = self._shorten(self.redactor.redact(evt.summary))
        detail = self._shorten(self.redactor.redact(evt.detail))
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
