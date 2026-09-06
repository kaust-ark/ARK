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
HANDOFF_NEXT_RE = re.compile(r'"next"\s*:\s*"([A-Za-z_][A-Za-z0-9_-]*)"')
HANDOFF_DONE_RE = re.compile(r'"done"\s*:\s*(true|false)', re.IGNORECASE)

# The hand-off is asked for as one plain last line, but an Agent that has just
# finished a 30-minute run writes it however it likes: bolded, bulleted, inside
# a fenced block, pretty-printed over several lines, with a full stop after the
# closing brace, or cut off mid-object when the output is truncated. A missing
# hand-off silently returns the route to the fixed successor table — the very
# behaviour the Room exists to replace — so the keyword is located first and
# the object is read from there rather than matched as a whole line.
HANDOFF_KEYWORD_RE = re.compile(r"HAND-?OFF", re.IGNORECASE)
# What may sit between the keyword and the object: a colon, markdown emphasis,
# a bullet, a quote, a code fence, the word "json". A letter anywhere else means
# the keyword was used in prose and the next brace belongs to something else.
HANDOFF_GAP_RE = re.compile(r"[\s:*_`>#\-]*(?:json)?[\s:*_`>#\-]*", re.IGNORECASE)
HANDOFF_GAP_LIMIT = 200

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


def _balanced_object(text: str, start: int) -> Optional[str]:
    """The JSON object beginning at ``text[start]``, or None if it never closes.

    Brace-counting rather than a regex, so a pretty-printed object spanning
    several lines and a brace inside a string value both survive.
    """
    depth = 0
    in_string = escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _handoff_candidates(output: str) -> list[str]:
    """Every stretch of text after a HANDOFF keyword that may hold a decision."""
    candidates: list[str] = []
    for match in HANDOFF_KEYWORD_RE.finditer(output):
        window = output[match.end():match.end() + HANDOFF_GAP_LIMIT]
        brace = window.find("{")
        if brace < 0 or not HANDOFF_GAP_RE.fullmatch(window[:brace]):
            continue
        # A truncated output leaves the object unclosed, but the window still
        # holds `"next": "writer"`, which is the part that decides the route.
        candidates.append(_balanced_object(output, match.end() + brace) or window[brace:])
    return candidates


def _handoff_payload(blob: str) -> dict:
    """Read a candidate as JSON, falling back to field-level regexes.

    Tolerates a trailing comma, single quotes, and an object that was cut off
    before its closing brace.
    """
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
    return payload if isinstance(payload, dict) else {}


def parse_handoff(agent_output: str) -> Optional[Handoff]:
    """Find what an Agent said about who should work next.

    Reads the *last* hand-off in the output, and reads it leniently, because a
    hand-off that is lost is worse than one that is parsed loosely and then
    validated against the team roster by
    :meth:`ark.sharednet.team.RoomTeam._decide`.
    """
    for blob in reversed(_handoff_candidates(agent_output or "")):
        payload = _handoff_payload(blob)
        if not payload:
            continue
        next_role = payload.get("next")
        if isinstance(next_role, str):
            next_role = next_role.strip().lower() or None
            if next_role in ("none", "null", "-"):
                next_role = None
        else:
            next_role = None
        done = bool(payload.get("done", False))
        reason = payload.get("reason", "")
        return Handoff(next=next_role, done=done,
                       reason=str(reason) if reason is not None else "")
    return None


HANDOFF_INSTRUCTION = """
## Hand-off (required)

You are one member of a team working in a shared Room. When you finish, you
decide what happens next. The **last line** of your final message must be
exactly this, as plain text:

    HANDOFF: {{"next": "<role>", "done": false, "reason": "<one sentence>"}}

Roles you may name: {roles}. Name the role whose work is now the bottleneck.
If the work is complete and nothing more should change, say
`{{"next": null, "done": true, "reason": "..."}}` instead.

That line must be the very last thing you write, on one line, not
pretty-printed, not inside a code fence, without bold or a bullet, and it must
appear nowhere else in your message. However long your message is, write it:
without it the team falls back to a fixed hand-off order and your judgement
about who should work next is discarded.
""".strip()


def handoff_instruction(roles: tuple[str, ...] | list[str]) -> str:
    return HANDOFF_INSTRUCTION.format(roles=", ".join(roles))
