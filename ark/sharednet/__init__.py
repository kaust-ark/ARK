"""ARK's research team as members of a SharedNet Room, with typed hand-offs.

* :mod:`ark.sharednet.room`  – ``join`` / ``send`` / ``wait`` over the V1 API
* :mod:`ark.sharednet.typed` – the typed envelope carried inside message content
* :mod:`ark.sharednet.team`  – the router: each Agent decides who works next, or done
* :mod:`ark.sharednet.ark_team` – binds the router to a live ``Orchestrator``

Run ``python -m ark.sharednet --help`` for the demo entry point.
"""

from .room import Invite, RoomClient, RoomError, RoomMessage
from .team import ARK_ROLES, DEFAULT_SUCCESSOR, HopContext, RoomTeam, TeamResult
from .typed import Envelope, Handoff, decode, encode, parse_handoff

__all__ = [
    "ARK_ROLES",
    "DEFAULT_SUCCESSOR",
    "Envelope",
    "Handoff",
    "HopContext",
    "Invite",
    "RoomClient",
    "RoomError",
    "RoomMessage",
    "RoomTeam",
    "TeamResult",
    "decode",
    "encode",
    "parse_handoff",
]
