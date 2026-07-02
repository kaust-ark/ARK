"""Approval gate — turn an ``ask`` decision into a human verdict.

The gate sits between the policy (what's risky) and a human (should it proceed).
It is channel-agnostic: today an ``ApprovalChannel`` relays over Telegram; a
webapp channel can be dropped in later without touching this file.

Key properties:

- **Fail-open.** With no channel (``NullChannel``), an ``ask`` auto-allows and is
  logged. Existing projects and the test suite are never blocked by a prompt no
  one can answer.
- **Approval memory.** A verdict can be remembered (this exact command shape, or
  the whole category) so the human isn't asked the same thing twice. Persisted
  to ``<state>/intervention_approvals.yaml``.
- **Safe default on timeout.** A blocking ask that times out resolves to *deny*
  (the agent's command fails and it adapts) — never a silent allow.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import yaml

from .policy import (
    Decision, InterventionPolicy, Category, Severity,
    ALLOW, NOTIFY, ASK, DENY,
)


# ── request / reply ────────────────────────────────────────

@dataclass
class ApprovalRequest:
    category: str
    severity: int
    reason: str
    detail: str                       # the command/action, already redacted
    consequence: str = ""
    origin: str = "agent"             # agent type or "orchestrator"
    timeout_seconds: int = 600

    def signature(self) -> str:
        """Stable key for approval memory: category + normalized action shape.

        Paths / numbers / hashes are stripped so ``rm -rf results/run_12`` and
        ``rm -rf results/run_98`` share one signature.
        """
        norm = self.detail.strip()
        norm = re.sub(r"[0-9a-f]{8,}", "X", norm)       # hashes/ids
        norm = re.sub(r"\d+", "N", norm)                 # numbers
        norm = re.sub(r"\s+", " ", norm)
        return f"{self.category}:{norm}"[:200]

    def card(self) -> str:
        """Human-readable approval card."""
        sev = Severity.name(self.severity)
        lines = [
            f"{self.origin} wants to: {self.reason}",
            f"• Command: {self.detail}",
        ]
        if self.consequence:
            lines.append(f"• Consequence: {self.consequence}")
        lines.append(f"• Category: {self.category} ({sev})")
        return "\n".join(lines)


@dataclass
class ApprovalReply:
    verdict: str                      # "approve" | "deny"
    remember: str = "none"            # "none" | "command" | "category"
    free_text: str = ""
    via: str = "human"               # "human" | "timeout" | "memory" | "failopen"


# Options surfaced to the human (kept aligned with ark.hitl option shape).
APPROVAL_OPTIONS = [
    {"id": "approve", "title": "Approve once", "consequence": "Run this action."},
    {"id": "deny", "title": "Deny", "consequence": "Refuse; the agent must find another way."},
    {"id": "approve_command", "title": "Approve & don't ask again for this command",
     "consequence": "Auto-approve identical commands for the rest of the run."},
    {"id": "approve_category", "title": "Approve & don't ask again for this category",
     "consequence": "Auto-approve this whole risk category for the rest of the run."},
]


# ── channels ───────────────────────────────────────────────

class ApprovalChannel(ABC):
    """Relays an approval request to a human and returns their verdict."""

    @abstractmethod
    def request(self, req: ApprovalRequest) -> Optional[ApprovalReply]:
        """Block until the human answers, or return None on timeout."""

    @abstractmethod
    def notify(self, text: str) -> None:
        """Send a non-blocking FYI (used for NOTIFY decisions)."""

    def request_secret(self, name: str, purpose: str, *, origin: str = "agent",
                       timeout: int = 1800) -> Optional[str]:
        """Ask a human for a secret VALUE (not a yes/no). Return the value, or
        None to deny / when no channel can supply it. Default: None (fail-open —
        the agent gets nothing and fails naturally rather than hanging)."""
        return None


class NullChannel(ApprovalChannel):
    """No human reachable → fail open (auto-approve) and just log."""

    def __init__(self, log_fn: Optional[Callable] = None):
        self._log = log_fn or (lambda *a, **k: None)

    def request(self, req: ApprovalRequest) -> Optional[ApprovalReply]:
        self._log(f"  [intervention] no approval channel — auto-allowing: {req.reason}", "WARN")
        return ApprovalReply(verdict="approve", via="failopen")

    def notify(self, text: str) -> None:
        self._log(f"  [intervention] {text}", "INFO")


class TelegramApprovalChannel(ApprovalChannel):
    """Relay over the orchestrator's existing Telegram dispatcher.

    The orchestrator supplies two callables so this module stays decoupled:
      ``ask_fn(question, options, timeout_s) -> {"option_id", "text"} | None``
      ``notify_fn(text) -> None``
    """

    def __init__(self, ask_fn: Callable, notify_fn: Callable, ask_secret_fn: Optional[Callable] = None):
        self._ask = ask_fn
        self._notify = notify_fn
        self._ask_secret = ask_secret_fn

    def request_secret(self, name: str, purpose: str, *, origin: str = "agent",
                       timeout: int = 1800) -> Optional[str]:
        if not self._ask_secret:
            return None
        try:
            val = self._ask_secret(name, purpose, timeout)
        except Exception:
            return None
        return val or None

    def request(self, req: ApprovalRequest) -> Optional[ApprovalReply]:
        resp = self._ask(req.card(), APPROVAL_OPTIONS, req.timeout_seconds)
        if not resp:
            return None
        opt = (resp.get("option_id") or "").strip()
        text = (resp.get("text") or "").strip()
        if opt == "deny":
            return ApprovalReply("deny", free_text=text)
        if opt == "approve_command":
            return ApprovalReply("approve", remember="command", free_text=text)
        if opt == "approve_category":
            return ApprovalReply("approve", remember="category", free_text=text)
        if opt == "approve":
            return ApprovalReply("approve", free_text=text)
        # Free-text-only reply: treat an explicit yes/no, else default deny.
        low = text.lower()
        if low in ("y", "yes", "ok", "approve", "allow", "go"):
            return ApprovalReply("approve", free_text=text)
        return ApprovalReply("deny", free_text=text)

    def notify(self, text: str) -> None:
        try:
            self._notify(text)
        except Exception:
            pass


# ── gate ───────────────────────────────────────────────────

class Gate:
    """Evaluate a policy ``Decision`` against the human + approval memory."""

    # After a deny (timeout or explicit), identical requests are auto-denied
    # without re-asking for this many seconds. Breaks the ask→timeout-deny→
    # agent-retries→ask… stall loop (each cycle used to block 10 min), while
    # still re-asking once per window in case the human simply missed the
    # first ask.
    DENY_MUTE_SECONDS = 1800

    def __init__(self, policy: InterventionPolicy, channel: ApprovalChannel,
                 state_dir: Optional[Path] = None, log_fn: Optional[Callable] = None):
        self.policy = policy
        self.channel = channel
        self.state_dir = Path(state_dir) if state_dir else None
        self._log = log_fn or (lambda *a, **k: None)
        self._mem = self._load_memory()
        # signature → (last_deny_monotonic, muted_repeat_count). Per-run only —
        # a denial is a point-in-time judgment, not project policy, so it is
        # deliberately NOT persisted to the approvals file.
        self._deny_mem: dict = {}

    # ---- public API ----

    def check_command(self, command, *, work_dirs=None, recent_job_count=0,
                      origin="agent") -> bool:
        d = self.policy.classify_command(
            command, work_dirs=work_dirs, recent_job_count=recent_job_count)
        return self._enforce(d, origin=origin)

    def check_action(self, action_type: str, *, origin="orchestrator", **ctx) -> bool:
        d = self.policy.classify_action(action_type, **ctx)
        return self._enforce(d, origin=origin)

    def request_secret(self, name: str, purpose: str, *, origin: str = "agent") -> Optional[str]:
        """Fetch a secret value from a human. Returns the value or None.

        The value is NEVER persisted or written to the audit trail — only the
        fact that ``name`` was requested and whether it was granted."""
        self._log(f"  [intervention] secret requested: {name} ({purpose}) — asking human", "WARN")
        value = self.channel.request_secret(name, purpose, origin=origin, timeout=1800)
        req = ApprovalRequest(category=Category.CREDENTIALS, severity=Severity.HIGH,
                              reason=f"secret request: {name}", detail=name,
                              consequence=purpose, origin=origin)
        self._record(req, ApprovalReply("approve" if value else "deny",
                                        via="human" if value else "timeout"))
        if value:
            self._log(f"  [intervention] secret '{name}' supplied (value redacted)", "INFO")
        else:
            self._log(f"  [intervention] secret '{name}' not supplied", "WARN")
        return value or None

    # ---- core ----

    def _enforce(self, d: Decision, origin: str) -> bool:
        """Return True to proceed, False to block."""
        if d.action == ALLOW:
            # Intentionally silent — allowed commands are the common case and
            # logging every one drowns the log. Only NOTIFY/ASK/DENY are shown.
            return True

        if d.action == NOTIFY:
            self._log(f"  [intervention] NOTIFY ({d.category}): {d.reason}", "WARN")
            self.channel.notify(f"ℹ️ {d.reason} — {d.detail}")
            return True

        if d.action == DENY:
            self._log(f"  [intervention] DENY ({d.category}): {d.reason}", "ERROR")
            return False

        if d.action != ASK:
            # Defensive: an unrecognized verb proceeds (logged), never silently blocks.
            self._log(f"  [intervention] unknown action '{d.action}', allowing", "WARN")
            return True

        # ASK ---------------------------------------------------------------
        req = ApprovalRequest(
            category=d.category or "unknown",
            severity=d.severity,
            reason=d.reason,
            detail=d.detail,
            consequence=d.consequence,
            origin=origin,
            timeout_seconds=self._timeout_for(d),
        )

        remembered = self._recall(req)
        if remembered is not None:
            self._log(f"  [intervention] {('approve' if remembered else 'deny')} from memory: {req.reason}", "INFO")
            self._record(req, ApprovalReply("approve" if remembered else "deny", via="memory"))
            return remembered

        # Deny-mute: an identical request was denied moments ago (timeout or
        # human). Auto-deny instantly instead of re-asking — otherwise a
        # retrying agent stalls the run in an ask→timeout→retry loop, burning
        # 10 minutes per cycle. The mute expires so the human still gets one
        # fresh ask per window.
        if self._deny_muted(req):
            last, n = self._deny_mem[req.signature()]
            self._deny_mem[req.signature()] = (last, n + 1)
            self._log(f"  [intervention] auto-DENY (repeat of a denied request, muted "
                      f"{self.DENY_MUTE_SECONDS//60}min): {req.reason}", "WARN")
            if n == 0:  # first muted repeat → tell the human once
                self.channel.notify(
                    f"🔇 The agent is retrying a denied action — auto-denying repeats "
                    f"for {self.DENY_MUTE_SECONDS//60} min so the run isn't stalled.\n"
                    f"• {req.reason}: {req.detail}\n"
                    f"To allow it, approve the next ask (after the mute) or steer the agent."
                )
            self._record(req, ApprovalReply("deny", via="memory"))
            return False

        self._log(f"  [intervention] ASK ({req.category}/{Severity.name(req.severity)}): {req.reason} → waiting for human", "WARN")
        reply = self.channel.request(req)
        if reply is None:
            # timeout → safe default = deny, and mute identical repeats
            self._log(f"  [intervention] timeout, safe-default DENY: {req.reason}", "WARN")
            self._note_denied(req)
            self._record(req, ApprovalReply("deny", via="timeout"))
            return False

        if reply.remember == "category":
            self._mem.setdefault("categories", {})[req.category] = (reply.verdict == "approve")
        elif reply.remember == "command":
            self._mem.setdefault("signatures", {})[req.signature()] = (reply.verdict == "approve")
        if reply.remember in ("category", "command"):
            self._save_memory()

        if reply.verdict != "approve":
            # A human "no" also mutes identical repeats — re-asking the same
            # thing minutes after an explicit deny is noise, not safety.
            self._note_denied(req)
        self._record(req, reply)
        return reply.verdict == "approve"

    # ---- deny-mute (anti-stall) ----

    def _note_denied(self, req: ApprovalRequest) -> None:
        import time as _time
        self._deny_mem[req.signature()] = (_time.monotonic(), 0)

    def _deny_muted(self, req: ApprovalRequest) -> bool:
        import time as _time
        entry = self._deny_mem.get(req.signature())
        if not entry:
            return False
        last, _n = entry
        if _time.monotonic() - last > self.DENY_MUTE_SECONDS:
            del self._deny_mem[req.signature()]
            return False
        return True

    def _timeout_for(self, d: Decision) -> int:
        # Credentials often need the human to fetch a value — give more time.
        return 1800 if d.category == Category.CREDENTIALS else 600

    # ---- approval memory ----

    def _recall(self, req: ApprovalRequest) -> Optional[bool]:
        cat = (self._mem.get("categories") or {}).get(req.category)
        if cat is not None:
            return bool(cat)
        sig = (self._mem.get("signatures") or {}).get(req.signature())
        if sig is not None:
            return bool(sig)
        return None

    def _memory_path(self) -> Optional[Path]:
        return (self.state_dir / "intervention_approvals.yaml") if self.state_dir else None

    def _load_memory(self) -> dict:
        p = self._memory_path()
        if p and p.exists():
            try:
                return yaml.safe_load(p.read_text()) or {}
            except Exception:
                return {}
        return {}

    def _save_memory(self) -> None:
        p = self._memory_path()
        if not p:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(yaml.safe_dump(self._mem, sort_keys=False, allow_unicode=True))
        except Exception as e:
            self._log(f"  [intervention] could not persist approval memory: {e}", "WARN")

    def _record(self, req: ApprovalRequest, reply: ApprovalReply) -> None:
        """Append to an audit trail (separate from the YAML memory)."""
        if not self.state_dir:
            return
        try:
            audit = self.state_dir / "intervention_audit.jsonl"
            audit.parent.mkdir(parents=True, exist_ok=True)
            import json
            entry = {
                "timestamp": datetime.now().isoformat(),
                "request": asdict(req),
                "verdict": reply.verdict,
                "via": reply.via,
                "remember": reply.remember,
            }
            with audit.open("a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass


def build_channel(kind: str, *, ask_fn=None, notify_fn=None, ask_secret_fn=None,
                  log_fn=None) -> ApprovalChannel:
    """Factory: 'telegram' (needs ask_fn+notify_fn) else NullChannel (fail-open)."""
    if kind == "telegram" and ask_fn and notify_fn:
        return TelegramApprovalChannel(ask_fn, notify_fn, ask_secret_fn=ask_secret_fn)
    return NullChannel(log_fn=log_fn)
