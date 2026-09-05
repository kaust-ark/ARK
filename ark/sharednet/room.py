"""A minimal client for a SharedNet Room: ``join``, ``send``, ``wait``.

SharedNet (https://sharednet.ai) exposes a Room as an append-only, sequenced
message log that coding Agents join with a Room-scoped invite token. The whole
Agent surface is three HTTP calls, and this module wraps exactly those, with
the standard library only, so it can run inside any project environment.

    POST /api/v1/rooms/{room}/join        Bearer rit_…   {"name": …}
    POST /api/v1/rooms/{room}/messages    Bearer rmt_…   {"content": …, "reply_to_message_id"?: …}
    GET  /api/v1/rooms/{room}/messages?after=N&limit=M
    GET  /api/v1/rooms/{room}/wait?after=N&timeout=S   (long-poll, 25 s cap)

Unlike the control-plane client (fail-soft), this client raises: the Room is
the coordination medium for :mod:`ark.sharednet.team`, and a failed send must
not be mistaken for a delivered hand-off.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

INVITE_TOKEN_RE = re.compile(r"\brit_[A-Za-z0-9_-]{43}\b")
MEMBER_TOKEN_RE = re.compile(r"\brmt_[A-Za-z0-9_-]{43}\b")
ROOM_ID_RE = re.compile(r"\brom_[A-Za-z0-9]+\b")
BASE_URL_RE = re.compile(r"\bBASE=(\S+)")
URL_ROOM_RE = re.compile(r"https?://[^\s/]+(?:/[^\s]*)?/rooms/(rom_[A-Za-z0-9]+)")

WAIT_MAX_SECONDS = 25
MAX_MESSAGE_BYTES = 32768


class RoomError(RuntimeError):
    """An HTTP error from the Room API, carrying SharedNet's error code."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(f"{status} {code}{(': ' + message) if message else ''}")
        self.status = status
        self.code = code


@dataclass
class Invite:
    """What a human pastes: BASE, ROOM and TOKEN, in any layout."""

    base_url: str
    room_id: str
    token: str

    @classmethod
    def parse(cls, text: str, default_base: str = "https://sharednet.ai") -> "Invite":
        token = INVITE_TOKEN_RE.search(text)
        room = ROOM_ID_RE.search(text)
        if not token or not room:
            raise ValueError("invite must contain a rom_ Room id and a rit_ invite token")
        base = default_base
        base_match = BASE_URL_RE.search(text)
        if base_match:
            base = base_match.group(1)
        else:
            url_match = URL_ROOM_RE.search(text)
            if url_match:
                base = url_match.group(0).split("/api/")[0].split("/rooms/")[0]
        return cls(base_url=base.rstrip("/"), room_id=room.group(0), token=token.group(0))


@dataclass
class RoomMessage:
    message_id: str
    sequence: int
    sender_id: str
    sender_name: Optional[str]
    content: str
    reply_to: Optional[str]
    created_at: str
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, item: dict) -> "RoomMessage":
        sender = item.get("sender") or {}
        return cls(
            message_id=item["id"],
            sequence=int(item["sequence"]),
            sender_id=sender.get("member_id") or item.get("sender_instance_id") or "",
            sender_name=sender.get("name"),
            content=item.get("content", ""),
            reply_to=item.get("reply_to_message_id"),
            created_at=item.get("created_at", ""),
            raw=item,
        )


Transport = Callable[[str, str, dict, Optional[dict], int], tuple[int, dict]]


def _urllib_transport(method: str, url: str, headers: dict, body: Optional[dict],
                      timeout: int) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            return response.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            parsed = {"error": {"code": "non_json_error", "message": payload[:200]}}
        return error.code, parsed


class RoomClient:
    """One member's view of one Room.

    Construct it with an invite and call :meth:`join`, or with a member token
    you kept from an earlier join. ``last_sequence`` is the only state a
    client needs to resume: everything after it is what it has not seen.
    """

    def __init__(self, base_url: str, room_id: str, *, token: Optional[str] = None,
                 timeout: int = WAIT_MAX_SECONDS + 10, transport: Optional[Transport] = None):
        self.base_url = base_url.rstrip("/")
        self.room_id = room_id
        self.token = token
        self.timeout = timeout
        self._transport = transport or _urllib_transport
        self.member_id: Optional[str] = None
        self.name: Optional[str] = None
        self.last_sequence = 0

    # ── HTTP ────────────────────────────────────────────────────────────────
    def _call(self, method: str, path: str, body: Optional[dict] = None, *,
              token: Optional[str] = None, timeout: Optional[int] = None) -> dict:
        headers = {"authorization": f"Bearer {token or self.token}"}
        if body is not None:
            headers["content-type"] = "application/json"
        status, payload = self._transport(method, f"{self.base_url}{path}", headers, body,
                                          timeout or self.timeout)
        if status >= 400:
            error = (payload or {}).get("error") or {}
            raise RoomError(status, error.get("code", f"http_{status}"), error.get("message", ""))
        return payload

    # ── The three verbs ─────────────────────────────────────────────────────
    def join(self, invite_token: str, name: str) -> list[RoomMessage]:
        """Join with an invite. Keeps the member token, returns the history so far."""
        payload = self._call("POST", f"/api/v1/rooms/{self.room_id}/join", {"name": name},
                             token=invite_token)
        self.token = payload["member_token"]
        membership = payload.get("membership") or {}
        self.member_id = membership.get("member_id")
        self.name = membership.get("name") or name
        history = [RoomMessage.from_json(item) for item in (payload.get("history") or {}).get("items", [])]
        if (payload.get("history") or {}).get("has_more"):
            history.extend(self.messages(after=history[-1].sequence if history else 0))
        self._advance(history)
        return history

    def send(self, content: str, reply_to: Optional[str] = None) -> RoomMessage:
        if len(content.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError(f"message exceeds {MAX_MESSAGE_BYTES} bytes")
        body: dict[str, Any] = {"content": content}
        if reply_to:
            body["reply_to_message_id"] = reply_to
        payload = self._call("POST", f"/api/v1/rooms/{self.room_id}/messages", body)
        message = RoomMessage.from_json(payload["message"])
        # A message of one's own is not "seen": the cursor moves only on read.
        return message

    def messages(self, after: int = 0, limit: int = 100) -> list[RoomMessage]:
        """Everything after ``after``, following pages until the end."""
        collected: list[RoomMessage] = []
        cursor = after
        while True:
            query = urllib.parse.urlencode({"after": cursor, "limit": min(limit, 100)})
            payload = self._call("GET", f"/api/v1/rooms/{self.room_id}/messages?{query}")
            items = [RoomMessage.from_json(item) for item in payload.get("items", [])]
            collected.extend(items)
            if not items or not payload.get("has_more"):
                break
            cursor = items[-1].sequence
        self._advance(collected)
        return collected

    def wait(self, after: Optional[int] = None, timeout: int = WAIT_MAX_SECONDS) -> list[RoomMessage]:
        """Sit in the Room until something is said after ``after`` (default: what
        this client has seen), or until ``timeout`` seconds (server cap 25)."""
        cursor = self.last_sequence if after is None else after
        query = urllib.parse.urlencode({"after": cursor, "timeout": max(0, min(timeout, WAIT_MAX_SECONDS))})
        payload = self._call("GET", f"/api/v1/rooms/{self.room_id}/wait?{query}",
                             timeout=timeout + 10)
        items = [RoomMessage.from_json(item) for item in payload.get("items", [])]
        self._advance(items)
        return items

    def room(self) -> dict:
        return self._call("GET", f"/api/v1/rooms/{self.room_id}")

    # ── Cursor ──────────────────────────────────────────────────────────────
    def _advance(self, items: list[RoomMessage]) -> None:
        for item in items:
            if item.sequence > self.last_sequence:
                self.last_sequence = item.sequence
