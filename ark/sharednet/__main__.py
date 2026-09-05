"""Run ARK's research team inside a SharedNet Room.

    # Demo, no model needed (scripted agents), against any Room invite:
    python -m ark.sharednet --invite "ROOM=rom_… TOKEN=rit_… BASE=https://sharednet.ai" --mock

    # A real project (same as `ark run` with a `sharednet:` block in config.yaml):
    python -m ark.sharednet --invite "…" --project myproject [--max-hops 12] [--start writer]

    # Watch a Room from the terminal (what a human sees in the Web):
    python -m ark.sharednet --invite "…" --tail
"""

from __future__ import annotations

import argparse
import sys

from .room import Invite, RoomClient
from .team import ARK_ROLES, RoomTeam
from .typed import decode


def _print_message(message) -> None:
    text, envelope = decode(message.content)
    tag = f" [{envelope.type}" + (f" → {envelope.get('next')}" if envelope and envelope.get("next") else "") + "]" if envelope else ""
    first = text.strip().split("\n")[0][:160]
    print(f"#{message.sequence:<4} {message.sender_name or message.sender_id:<18}{tag}\n      {first}")


def cmd_tail(invite: Invite, name: str) -> int:
    client = RoomClient(invite.base_url, invite.room_id)
    history = client.join(invite.token, name)
    for message in history:
        _print_message(message)
    print(f"-- watching {invite.room_id} as {name}; Ctrl-C to stop --", flush=True)
    try:
        while True:
            for message in client.wait(timeout=25):
                _print_message(message)
    except KeyboardInterrupt:
        return 0


def cmd_mock(invite: Invite, goal: str, start: str, max_hops: int, delay: float) -> int:
    from collections import Counter

    from .mock import scripted_team
    from .typed import WORK_RESULT

    team = RoomTeam(invite.base_url, invite.room_id, invite.token, lambda role, task: "",
                    roles=("experimenter", "coder", "writer", "reviewer", "planner"),
                    coordinator_name="ark-orchestrator (mock)", max_hops=max_hops)
    history = team.join_all()
    played = Counter()
    for message in history:
        _, envelope = decode(message.content)
        if envelope is not None and envelope.type == WORK_RESULT and message.sender_name in team.roles:
            played[message.sender_name] += 1
    team.run_agent = scripted_team(delay_seconds=delay, already_played=dict(played))
    result = team.run(goal, start_role=start)
    print(f"\n{'DONE' if result.done else 'STOPPED'}: {result.reason}\nroute: {' → '.join(result.route)}")
    return 0 if result.done else 2


def cmd_project(invite_text: str, project: str, start: str | None, max_hops: int, goal: str | None) -> int:
    from ark.orchestrator import Orchestrator

    from .ark_team import run_room_team

    orch = Orchestrator(project=project)
    settings = dict(orch.config.get("sharednet") or {})
    settings["invite"] = invite_text
    settings["max_hops"] = max_hops
    if start:
        settings["start_role"] = start
    if goal:
        settings["goal"] = goal
    result = run_room_team(orch, settings)
    return 0 if result.done else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ark.sharednet", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--invite", required=True, help="the pasted invite: ROOM=… TOKEN=… [BASE=…]")
    parser.add_argument("--base", default=None, help="override the base URL (default: from the invite, else https://sharednet.ai)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", help="scripted agents; proves the loop without a model")
    mode.add_argument("--project", help="an ARK project name; runs the real agents")
    mode.add_argument("--tail", action="store_true", help="only watch the Room")
    parser.add_argument("--goal", default="Turn the project idea into a submission-ready paper.")
    parser.add_argument("--start", default=None, choices=ARK_ROLES, help="first role to work")
    parser.add_argument("--max-hops", type=int, default=12)
    parser.add_argument("--delay", type=float, default=0.0, help="mock only: seconds per hop, to watch it live")
    parser.add_argument("--name", default="observer", help="tail only: member name")
    args = parser.parse_args(argv)

    invite = Invite.parse(args.invite)
    if args.base:
        invite.base_url = args.base.rstrip("/")

    if args.tail:
        return cmd_tail(invite, args.name)
    if args.mock:
        return cmd_mock(invite, args.goal, args.start or "experimenter", args.max_hops, args.delay)
    if args.project:
        invite_text = f"ROOM={invite.room_id} TOKEN={invite.token} BASE={invite.base_url}"
        return cmd_project(invite_text, args.project, args.start, args.max_hops,
                           args.goal if args.goal != parser.get_default("goal") else None)
    parser.error("choose one of --mock, --project NAME, or --tail")
    return 2


if __name__ == "__main__":
    sys.exit(main())
