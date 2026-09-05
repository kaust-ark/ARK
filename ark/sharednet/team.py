"""Run a team of role Agents as members of one SharedNet Room.

ARK's six roles (researcher, reviewer, planner, writer, experimenter, coder)
today hand work to each other through files and a fixed Python loop. Here the
same roles sit in a Room as separate members, every hand-off is a typed
message in the Room log, and *the Agent that just finished decides who works
next, or that the work is done*. The orchestrator only enforces guard-rails.

One hop:

    coordinator  ──work.request──▶  Room        ("@writer <task>")
    writer runs  (run_agent("writer", task + hand-off instruction))
    writer       ──work.result───▶  Room        (summary + {"next": "reviewer", "done": false})
    coordinator picks the next role from that decision (or from policy when the
    decision is missing or invalid), and repeats until ``done`` or the hop cap.

Anything a human (or another Agent) says in the Room between hops is folded
into the next task as "Room guidance", so the Room is a real group chat, not a
log viewer. The Room is also the resume point: a new process reads the log,
finds the last ``work.result`` and continues from its ``next``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from .room import RoomClient, RoomMessage
from .typed import (
    DONE,
    STOPPED,
    WORK_REQUEST,
    WORK_RESULT,
    Envelope,
    Handoff,
    decode,
    encode,
    handoff_instruction,
    parse_handoff,
)

ARK_ROLES: tuple[str, ...] = ("researcher", "experimenter", "coder", "writer", "reviewer", "planner")

# Who works next when an Agent gives no usable decision. Mirrors ARK's fixed
# loop: results → draft → review → plan → revise → review …
DEFAULT_SUCCESSOR: dict[str, str] = {
    "researcher": "experimenter",
    "experimenter": "writer",
    "coder": "experimenter",
    "writer": "reviewer",
    "reviewer": "planner",
    "planner": "writer",
}

RunAgent = Callable[[str, str], str]
TaskBuilder = Callable[["HopContext"], str]
DoneGuard = Callable[[str, str], Optional[bool]]
Log = Callable[[str], None]

SUMMARY_HEAD = 5000
SUMMARY_TAIL = 1500


@dataclass
class HopContext:
    """What a task builder sees when composing the next request."""

    role: str
    goal: str
    hop: int
    guidance: list[RoomMessage]
    previous_role: Optional[str]
    previous_output: str
    previous_decision: Optional[Handoff]


@dataclass
class Decision:
    next: Optional[str]
    done: bool
    reason: str
    decided_by: str  # "agent" | "policy"


@dataclass
class Hop:
    hop: int
    role: str
    request: RoomMessage
    result: RoomMessage
    output: str
    decision: Decision


@dataclass
class TeamResult:
    done: bool
    reason: str
    hops: list[Hop] = field(default_factory=list)

    @property
    def route(self) -> list[str]:
        return [hop.role for hop in self.hops]


def summarize(output: str) -> str:
    text = (output or "").strip() or "(no output)"
    if len(text) <= SUMMARY_HEAD + SUMMARY_TAIL:
        return text
    return f"{text[:SUMMARY_HEAD]}\n\n… ({len(text) - SUMMARY_HEAD - SUMMARY_TAIL} chars elided) …\n\n{text[-SUMMARY_TAIL:]}"


def default_task_builder(context: HopContext) -> str:
    parts = [f"Goal: {context.goal}"]
    if context.previous_role:
        parts.append(
            f"You were handed this by {context.previous_role}"
            f"{(': ' + context.previous_decision.reason) if context.previous_decision and context.previous_decision.reason else '.'}"
        )
    parts.append(f"Do the {context.role}'s part of the work now.")
    return "\n".join(parts)


class RoomTeam:
    """The roster, the Room, and the routing rules."""

    def __init__(
        self,
        base_url: str,
        room_id: str,
        invite_token: str,
        run_agent: RunAgent,
        *,
        roles: Iterable[str] = ARK_ROLES,
        coordinator_name: str = "ark-orchestrator",
        member_prefix: str = "",
        successor: Optional[dict[str, str]] = None,
        task_builder: TaskBuilder = default_task_builder,
        done_guard: Optional[DoneGuard] = None,
        max_hops: int = 12,
        max_consecutive_same_role: int = 2,
        steer_wait_seconds: int = 0,
        log: Log = print,
        client_factory: Callable[[str, str], RoomClient] = RoomClient,
    ):
        self.roles = tuple(roles)
        if not self.roles:
            raise ValueError("a team needs at least one role")
        self.base_url = base_url
        self.room_id = room_id
        self.invite_token = invite_token
        self.run_agent = run_agent
        self.coordinator_name = coordinator_name
        self.member_prefix = member_prefix
        self.successor = dict(successor or DEFAULT_SUCCESSOR)
        self.task_builder = task_builder
        self.done_guard = done_guard
        self.max_hops = max_hops
        self.max_consecutive_same_role = max_consecutive_same_role
        self.steer_wait_seconds = steer_wait_seconds
        self.log = log
        self._client_factory = client_factory
        self.coordinator: Optional[RoomClient] = None
        self.members: dict[str, RoomClient] = {}
        self._seen = 0

    # ── Membership ──────────────────────────────────────────────────────────
    def join_all(self) -> list[RoomMessage]:
        """Every role joins as its own member so the Room shows who said what."""
        self.coordinator = self._client_factory(self.base_url, self.room_id)
        history = self.coordinator.join(self.invite_token, self.coordinator_name)
        for role in self.roles:
            client = self._client_factory(self.base_url, self.room_id)
            client.join(self.invite_token, f"{self.member_prefix}{role}")
            self.members[role] = client
        self._seen = self._last_typed_sequence(history)
        self.log(f"[room] joined {self.room_id} as {self.coordinator_name} + {len(self.roles)} roles; "
                 f"{len(history)} message(s) of history")
        return history

    @property
    def member_ids(self) -> set[str]:
        ids = {client.member_id for client in self.members.values() if client.member_id}
        if self.coordinator and self.coordinator.member_id:
            ids.add(self.coordinator.member_id)
        return ids

    # ── Resume ──────────────────────────────────────────────────────────────
    def resume_point(self, history: list[RoomMessage]) -> tuple[Optional[str], int, bool]:
        """(next role, hops so far, already done) from the Room log alone.

        A ``stopped`` message (hop cap) is a pause, not an end: the next process
        continues from the last ``work.result``'s decision.
        """
        hops = 0
        next_role: Optional[str] = None
        for message in history:
            _, envelope = decode(message.content)
            if envelope is None:
                continue
            if envelope.type == DONE:
                return None, hops, True
            if envelope.type == WORK_RESULT:
                hops += 1
                next_role = envelope.get("next")
                if envelope.get("done"):
                    return None, hops, True
        return next_role, hops, False

    @staticmethod
    def _last_typed_sequence(history: list[RoomMessage]) -> int:
        """Untyped messages after the last typed one are guidance not yet delivered."""
        last = 0
        for message in history:
            _, envelope = decode(message.content)
            if envelope is not None:
                last = max(last, message.sequence)
        return last

    # ── The loop ────────────────────────────────────────────────────────────
    def run(self, goal: str, start_role: Optional[str] = None) -> TeamResult:
        if self.coordinator is None:
            history = self.join_all()
        else:
            history = self.coordinator.messages(after=0)
            self._seen = self._last_typed_sequence(history)

        resumed_next, hop, already_done = self.resume_point(history)
        if already_done:
            self.log("[room] the log already ends with done; nothing to do")
            return TeamResult(done=True, reason="already done in Room log")
        role = resumed_next if resumed_next in self.roles else (start_role or self.roles[0])
        if role not in self.roles:
            raise ValueError(f"start role {role!r} is not in the team {self.roles}")
        if hop:
            self.log(f"[room] resuming after {hop} hop(s): next is {role}")

        hops: list[Hop] = []
        previous_role: Optional[str] = None
        previous_output = ""
        previous_decision: Optional[Handoff] = None
        consecutive = 0  # how many hops in a row the current role has run, this one included

        while hop < self.max_hops:
            hop += 1
            consecutive = consecutive + 1 if role == previous_role else 1
            guidance = self._collect_guidance()
            context = HopContext(role=role, goal=goal, hop=hop, guidance=guidance,
                                 previous_role=previous_role, previous_output=previous_output,
                                 previous_decision=previous_decision)
            task = self.task_builder(context)
            if guidance:
                lines = "\n".join(f"- {m.sender_name or m.sender_id}: {m.content.strip()}" for m in guidance)
                task = f"{task}\n\n## Room guidance (said in the Room since the last hand-off)\n{lines}"

            request = self.coordinator.send(
                encode(f"@{role} hop {hop}\n\n{task}", Envelope(WORK_REQUEST, {"to": role, "hop": hop}))
            )
            self.log(f"[room] hop {hop}: → {role} (seq {request.sequence})")

            output = self.run_agent(role, f"{task}\n\n{handoff_instruction(self.roles)}")
            handoff = parse_handoff(output)
            decision = self._decide(role, handoff, output, consecutive)

            result_fields = {"next": decision.next, "done": decision.done, "reason": decision.reason,
                             "decided_by": decision.decided_by, "hop": hop}
            if self.done_guard is not None and role == "reviewer":
                score = _extract_score(output)
                if score is not None:
                    result_fields["score"] = score
            result_text = summarize(output)
            trailer = ("Done." if decision.done else f"Next: {decision.next}") + (
                f" ({decision.reason})" if decision.reason else "")
            result = self.members[role].send(
                encode(f"{result_text}\n\n{trailer}", Envelope(WORK_RESULT, result_fields)),
                reply_to=request.message_id,
            )
            # _seen stays where guidance collection left it: anything said in the
            # Room while the Agent worked (sequence between request and result)
            # must still be read at the next hop.
            hops.append(Hop(hop=hop, role=role, request=request, result=result, output=output,
                            decision=decision))
            self.log(f"[room] hop {hop}: {role} → {'done' if decision.done else decision.next} "
                     f"[{decision.decided_by}] {decision.reason}")

            if decision.done:
                self.coordinator.send(encode(f"Run finished after {hop} hop(s): {decision.reason}",
                                             Envelope(DONE, {"reason": decision.reason, "hops": hop})))
                return TeamResult(done=True, reason=decision.reason, hops=hops)

            previous_role, previous_output, previous_decision = role, output, handoff
            role = decision.next

        reason = f"hop cap {self.max_hops} reached; next would be {role}"
        self.coordinator.send(encode(f"Run stopped: {reason}. Resume by running the team again in this Room.",
                                     Envelope(STOPPED, {"reason": reason, "hops": hop, "next": role})))
        return TeamResult(done=False, reason=reason, hops=hops)

    # ── Routing policy ──────────────────────────────────────────────────────
    def _decide(self, role: str, handoff: Optional[Handoff], output: str, consecutive: int) -> Decision:
        fallback = self.successor.get(role) or self.roles[0]
        if handoff is None:
            return Decision(next=fallback, done=False, reason="no HANDOFF line; policy successor",
                            decided_by="policy")
        if handoff.done:
            verdict = self.done_guard(role, output) if self.done_guard else None
            if verdict is False:
                return Decision(next=fallback, done=False,
                                reason=f"agent said done but the guard disagrees; {handoff.reason}".strip("; "),
                                decided_by="policy")
            return Decision(next=None, done=True, reason=handoff.reason or "agent declared done",
                            decided_by="agent")
        if handoff.next not in self.roles:
            return Decision(next=fallback, done=False,
                            reason=f"asked for {handoff.next!r}, not a team role; policy successor",
                            decided_by="policy")
        if handoff.next == role and consecutive >= self.max_consecutive_same_role:
            return Decision(next=fallback, done=False,
                            reason=f"{role} has run {consecutive} hops in a row and asked for itself again; policy successor",
                            decided_by="policy")
        return Decision(next=handoff.next, done=False, reason=handoff.reason, decided_by="agent")

    # ── Group chat: what others said between hops ───────────────────────────
    def _collect_guidance(self) -> list[RoomMessage]:
        assert self.coordinator is not None
        fresh = self.coordinator.messages(after=self._seen)
        if not fresh and self.steer_wait_seconds > 0:
            deadline = time.monotonic() + self.steer_wait_seconds
            while time.monotonic() < deadline:
                fresh = self.coordinator.wait(after=self._seen,
                                              timeout=max(1, min(25, int(deadline - time.monotonic()))))
                if fresh:
                    break
        if fresh:
            self._seen = max(self._seen, fresh[-1].sequence)
        team = self.member_ids
        guidance = []
        for message in fresh:
            _, envelope = decode(message.content)
            if envelope is None and message.sender_id not in team:
                guidance.append(message)
        return guidance


def _extract_score(output: str) -> Optional[float]:
    import re

    match = re.search(r"Overall Score[：:]\s*(\d+\.?\d*)/10", output or "", re.IGNORECASE)
    return float(match.group(1)) if match else None
