"""An in-process stand-in for the SharedNet V1 Room API, for tests.

Implements exactly what :mod:`ark.sharednet.room` uses — ``join`` with an
invite, ``messages``, ``wait`` and ``GET room`` — with the same JSON shapes and
error codes as ``packages/server/src/handler.ts`` in the SharedNet repository.
Everything else is out of scope; a real run uses the real server.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"


def _token(prefix: str) -> str:
    return prefix + "".join(secrets.choice(ALPHABET) for _ in range(43))


class FakeRoomStore:
    def __init__(self, room_id: str = "rom_fake0000001"):
        self.room_id = room_id
        self.invite = _token("rit_")
        self.revoked = False
        self.members: dict[str, dict] = {}  # rmt_ token → membership
        self.messages: list[dict] = []
        self.lock = threading.Condition()
        self._counter = 0

    def _id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._counter:012d}"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def join(self, name: str) -> dict:
        token = _token("rmt_")
        membership = {
            "room_id": self.room_id,
            "member_id": self._id("mem"),
            "kind": "guest",
            "name": name,
            "agent_id": None,
            "instance_id": None,
            "invited_by_principal_id": "p_owner",
            "state": "active",
            "joined_at": self._now(),
            "left_at": None,
        }
        with self.lock:
            self.members[token] = membership
            items = list(self.messages)
        return {
            "room": {"id": self.room_id, "name": "fake", "state": "open"},
            "membership": membership,
            "member_token": token,
            "history": {"items": items, "next_cursor": None, "has_more": False},
        }

    def post(self, member: dict, content: str, reply_to: str | None) -> dict:
        if not content.strip() or len(content.encode("utf-8")) > 32768:
            raise ValueError("invalid_request")
        with self.lock:
            if reply_to and not any(m["id"] == reply_to for m in self.messages):
                raise ValueError("invalid_request")
            message = {
                "id": self._id("msg"),
                "room_id": self.room_id,
                "sequence": len(self.messages) + 1,
                "sender_principal_id": "p_owner",
                "sender_agent_id": None,
                "sender_instance_id": None,
                "sender": {"member_id": member["member_id"], "kind": "guest", "name": member["name"]},
                "type": "message",
                "content": content,
                "reply_to_message_id": reply_to,
                "created_at": self._now(),
            }
            self.messages.append(message)
            self.lock.notify_all()
        return message

    def page(self, after: int, limit: int) -> dict:
        with self.lock:
            items = [m for m in self.messages if m["sequence"] > after][:limit]
            has_more = len([m for m in self.messages if m["sequence"] > after]) > limit
        return {"items": items, "next_cursor": None, "has_more": has_more}

    def wait(self, after: int, limit: int, timeout_s: float) -> dict:
        deadline = time.monotonic() + timeout_s
        with self.lock:
            while not any(m["sequence"] > after for m in self.messages):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.lock.wait(min(remaining, 0.25))
        return self.page(after, limit)

    # Test helpers ------------------------------------------------------------
    def speak_as_human(self, name: str, content: str) -> dict:
        """A human typing in the Web UI: a member not in the team."""
        member = self.join(name)["membership"]
        return self.post(member, content, None)

    def transcript(self) -> list[dict]:
        with self.lock:
            return list(self.messages)


class _Handler(BaseHTTPRequestHandler):
    store: FakeRoomStore

    def log_message(self, *args):  # silence
        pass

    def _send(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str):
        self._send(status, {"error": {"code": code, "message": code}})

    def _bearer(self) -> str:
        auth = self.headers.get("authorization", "")
        return auth[len("Bearer "):] if auth.startswith("Bearer ") else ""

    def _member(self) -> dict | None:
        return self.store.members.get(self._bearer())

    def _json_body(self) -> dict:
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    def do_POST(self):
        url = urlparse(self.path)
        parts = url.path.strip("/").split("/")
        if len(parts) == 5 and parts[:3] == ["api", "v1", "rooms"] and parts[4] == "join":
            if parts[3] != self.store.room_id:
                return self._error(404, "room_not_found")
            token = self._bearer()
            if token != self.store.invite:
                return self._error(401, "invalid_token")
            if self.store.revoked:
                return self._error(410, "invite_revoked")
            body = self._json_body()
            name = body.get("name")
            if not isinstance(name, str) or not name.strip():
                return self._error(400, "invalid_request")
            return self._send(200, self.store.join(name.strip()))
        if len(parts) == 5 and parts[:3] == ["api", "v1", "rooms"] and parts[4] == "messages":
            member = self._member()
            if member is None:
                return self._error(401, "invalid_token")
            body = self._json_body()
            try:
                message = self.store.post(member, body.get("content", ""), body.get("reply_to_message_id"))
            except ValueError as error:
                return self._error(400, str(error))
            return self._send(201, {"message": message})
        return self._error(404, "route_not_found")

    def do_GET(self):
        url = urlparse(self.path)
        parts = url.path.strip("/").split("/")
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        member = self._member()
        if member is None:
            return self._error(401, "invalid_token")
        if len(parts) == 4 and parts[:3] == ["api", "v1", "rooms"]:
            return self._send(200, {"room": {"id": self.store.room_id, "state": "open"},
                                    "memberships": list(self.store.members.values())})
        if len(parts) == 5 and parts[4] in ("messages", "wait"):
            after = int(query.get("after", 0))
            limit = int(query.get("limit", 50))
            if parts[4] == "messages":
                return self._send(200, self.store.page(after, limit))
            timeout = min(int(query.get("timeout", 25)), 25)
            return self._send(200, self.store.wait(after, limit, timeout))
        return self._error(404, "route_not_found")


class FakeSharedNet:
    """``with FakeSharedNet() as fake: fake.base_url, fake.store.invite``."""

    def __init__(self):
        self.store = FakeRoomStore()
        handler = type("BoundHandler", (_Handler,), {"store": self.store})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def room_id(self) -> str:
        return self.store.room_id

    @property
    def invite(self) -> str:
        return self.store.invite

    def __enter__(self) -> "FakeSharedNet":
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
