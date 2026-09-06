"""A long-lived SharedNet dev Room on this machine.

The Room API is a message log; ARK's RAC layer (ark/sharednet/) is the part
under test, and it speaks plain HTTP. This serves the same V1 surface from
tests/fake_sharednet.py on a fixed port so several `ark run` processes — and a
human with `--tail` — can share one Room without a sharednet.ai account.

    python scripts/rac/dev_room.py --port 50397 [--room rom_devroom01]

Prints the invite line to stdout and to --invite-file, then serves until Ctrl-C.
In memory only: the log dies with this process, so keep it up for the whole run.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from tests.fake_sharednet import FakeRoomStore, _Handler  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=50397)
    parser.add_argument("--room", default="rom_devroom01")
    parser.add_argument("--invite-file", default="")
    parser.add_argument("--transcript", default="", help="write the Room log here on exit")
    args = parser.parse_args()

    store = FakeRoomStore(room_id=args.room)
    handler = type("BoundHandler", (_Handler,), {"store": store})
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    invite = f"ROOM={store.room_id} TOKEN={store.invite} BASE={base}"

    print(f'SharedNet dev Room "{args.room}" is open (in memory, this process only).\n')
    print(invite, flush=True)
    if args.invite_file:
        pathlib.Path(args.invite_file).write_text(invite + "\n")
        print(f"\ninvite written to {args.invite_file}")
    print("\nCtrl-C to close the Room.\n", flush=True)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        if args.transcript:
            import json
            pathlib.Path(args.transcript).write_text(json.dumps(store.transcript(), indent=2))
            print(f"transcript ({len(store.messages)} messages) → {args.transcript}")
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
