"""Typed messages on top of a plain SharedNet Room.

SharedNet V1 stores ``content`` and ``reply_to_message_id``; its ``type`` field
is reserved for V2 (``work.request``, ``work.accept``, …). Until that lands in
the API, the type travels *inside* the content, the same way SharedNet's own
coordination tags (``delegate-to:<member>``, ``human-review-required``) do: a
human reads the text, a program reads the trailer.

Wire format (one message):

    <human-readable text>

    sharednet-typed: {"type": "work.result", "next": "writer", "done": false, ...}

The trailer is the last non-empty line. Anything without it is an untyped
message: a human speaking in the Room, or a foreign Agent.

Types used by :mod:`ark.sharednet.team`:

    work.request   coordinator → role     {"to": role, "hop": n, "task": …}
    work.result    role → Room            {"next": role|null, "done": bool, "reason": …,
                                           "decided_by": "agent"|"policy", "score"?: float}
    done           coordinator → Room     {"reason": …, "hops": n}   the work is finished
    stopped        coordinator → Room     {"reason": …, "hops": n}   paused by a cap; resumable
    human_review   any → Room             {"question": …}   (also tagged human-review-required)

An Agent states its own decision by ending its final message with one line:

    HANDOFF: {"next": "writer", "done": false, "reason": "results are in; draft §4"}

``next`` must be a role in the team, or ``null`` when ``done`` is true.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

TRAILER_PREFIX = "sharednet-typed: "
TRAILER_RE = re.compile(r"^sharednet-typed: (\{.*\})\s*$")
HANDOFF_RE = re.compile(r"HANDOFF:\s*(\{.*?\})\s*$", re.MULTILINE)
HANDOFF_NEXT_RE = re.compile(r'"next"\s*:\s*"([A-Za-z_][A-Za-z0-9_-]*)"')
HANDOFF_DONE_RE = re.compile(r'"done"\s*:\s*(true|false)', re.IGNORECASE)

WORK_REQUEST = "work.request"
WORK_RESULT = "work.result"
DONE = "done"
STOPPED = "stopped"
HUMAN_REVIEW = "human_review"
KNOWN_TYPES = (WORK_REQUEST, WORK_RESULT, DONE, STOPPED, HUMAN_REVIEW)

HUMAN_REVIEW_TAG = "human-review-required"


@dataclass
class Envelope:
    """The machine-readable part of a typed message."""

    type: str
    fields: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.fields.get(key, default)

    def to_json(self) -> str:
        payload = {"type": self.type, **self.fields}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> Optional["Envelope"]:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            return None
        fields = {key: value for key, value in payload.items() if key != "type"}
        return cls(type=payload["type"], fields=fields)


def encode(text: str, envelope: Envelope) -> str:
    """Attach ``envelope`` to ``text`` as the trailer line."""
    body = text.rstrip()
    if envelope.type == HUMAN_REVIEW and HUMAN_REVIEW_TAG not in body:
        body = f"{body}\n\n{HUMAN_REVIEW_TAG}"
    return f"{body}\n\n{TRAILER_PREFIX}{envelope.to_json()}"


def decode(content: str) -> tuple[str, Optional[Envelope]]:
    """Split a message into its text and its envelope (``None`` when untyped)."""
    lines = content.rstrip().split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return content, None
    match = TRAILER_RE.match(lines[-1].strip())
    if not match:
        return content, None
    envelope = Envelope.from_json(match.group(1))
    if envelope is None:
        return content, None
    return "\n".join(lines[:-1]).rstrip(), envelope


@dataclass
class Handoff:
    """What an Agent said about who should work next."""

    next: Optional[str]
    done: bool
    reason: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def parse_handoff(agent_output: str) -> Optional[Handoff]:
    """Find the last ``HANDOFF: {...}`` line in an Agent's output.

    Tolerates slightly broken JSON (a trailing comma, single quotes) by falling
    back to field-level regexes, because a hand-off that is lost is worse than
    one that is parsed leniently and then validated against the team roster.
    """
    matches = list(HANDOFF_RE.finditer(agent_output or ""))
    if not matches:
        return None
    blob = matches[-1].group(1)
    try:
        payload = json.loads(blob)
    except json.JSONDecodeError:
        payload = {}
        next_match = HANDOFF_NEXT_RE.search(blob.replace("'", '"'))
        done_match = HANDOFF_DONE_RE.search(blob)
        if next_match:
            payload["next"] = next_match.group(1)
        if done_match:
            payload["done"] = done_match.group(1).lower() == "true"
        if not payload:
            return None
    if not isinstance(payload, dict):
        return None
    next_role = payload.get("next")
    if isinstance(next_role, str):
        next_role = next_role.strip().lower() or None
        if next_role in ("none", "null", "-"):
            next_role = None
    else:
        next_role = None
    done = bool(payload.get("done", False))
    reason = payload.get("reason", "")
    return Handoff(next=next_role, done=done, reason=str(reason) if reason is not None else "")


HANDOFF_INSTRUCTION = """
## Hand-off (required)

You are one member of a team working in a shared Room. When you finish, you
decide what happens next. End your final message with exactly one line:

    HANDOFF: {{"next": "<role>", "done": false, "reason": "<one sentence>"}}

Roles you may name: {roles}. Name the role whose work is now the bottleneck.
If the work is complete and nothing more should change, say
`{{"next": null, "done": true, "reason": "..."}}` instead. Do not write this
line anywhere else in your message.
""".strip()


def handoff_instruction(roles: tuple[str, ...] | list[str]) -> str:
    return HANDOFF_INSTRUCTION.format(roles=", ".join(roles))
