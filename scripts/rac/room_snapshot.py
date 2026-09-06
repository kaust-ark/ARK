"""Copy each Room's message log to disk, repeatedly, while the runs are live.

The Room log is the primary process evidence for the runtime arm: every hop's
`work.result` carries `next`, `done`, `decided_by`, `score` and the agent's
stated reason, which is what the routing analysis is computed from. But the
local Room server keeps that log in memory and only writes it out on Ctrl-C,
so a SIGKILL — or the machine going down — would take the evidence with it.

Reading is safe to do while a run is in flight: joining a Room registers a
membership without appending a message, so nothing the Agents see changes.

    python scripts/rac/room_snapshot.py --interval 120
    python scripts/rac/room_snapshot.py --once
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ark.sharednet.room import Invite, RoomClient  # noqa: E402
from ark.sharednet.typed import decode  # noqa: E402

OUT_DIR = pathlib.Path(__file__).resolve().parent / "room_logs"
INVITES = {
    "finance": pathlib.Path("/tmp/rac4_invite_finance.txt"),
    "scsl": pathlib.Path("/tmp/rac4_invite_scsl.txt"),
    "bialign2": pathlib.Path("/tmp/rac4_invite_bialign.txt"),
}


def snapshot(name: str, invite_file: pathlib.Path) -> str:
    invite = Invite.parse(invite_file.read_text())
    client = RoomClient(invite.base_url, invite.room_id)
    client.join(invite.token, f"snapshot-{name}")
    messages, after = [], 0
    while True:
        batch = client.messages(after=after, limit=100)
        if not batch:
            break
        messages.extend(batch)
        after = batch[-1].sequence
    records = []
    for message in messages:
        text, envelope = decode(message.content)
        records.append({
            "sequence": message.sequence,
            "sender": message.sender_name or message.sender_id,
            "created_at": message.created_at,
            "type": envelope.type if envelope else None,
            "fields": dict(envelope.fields) if envelope else None,
            "text": text,
        })
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{name}.json").write_text(json.dumps(records, indent=2))
    hops = sum(1 for r in records if r["type"] == "work.result")
    return f"{name}: {len(records)} messages, {hops} hop(s) → {OUT_DIR / f'{name}.json'}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=120)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    while True:
        for name, invite_file in INVITES.items():
            try:
                print(time.strftime("%H:%M:%S"), snapshot(name, invite_file), flush=True)
            except Exception as error:
                print(time.strftime("%H:%M:%S"), f"{name}: {type(error).__name__}: {error}", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
