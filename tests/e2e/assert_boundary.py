#!/usr/bin/env python3
"""Assert the container e2e drove the /v1 boundary correctly.

Runs INSIDE the webapp container (DB access) AFTER the isolated job container has
finished its orchestrator run. It verifies — from the control plane's side — that
everything the orchestrator was supposed to push over HTTP actually landed, with
NO shared filesystem/DB between the two containers:

  1. status      — the run reported itself running and then reached a terminal
                   state (done/error/finished), i.e. bootstrap + report_status
                   round-tripped.
  2. events      — live-log lines were stored via POST /v1/.../events (this is
                   the "no shared FS for logs" guarantee).
  3. command     — the seeded set_autonomy command was pulled + acked (no longer
                   pending) and applied (autonomy_level flipped). Loop mode only.
  4. auth        — a token scoped to a DIFFERENT project is rejected 403 by /v1.

Usage:
    python assert_boundary.py <project_id> [--expect-command-ack]
"""

import sys
import urllib.error
import urllib.request

from website.dashboard.config import get_settings
from website.dashboard import db
from website.dashboard.auth import make_job_token

SEED_AUTONOMY = "full_auto"
TERMINAL = {"done", "finished", "error", "completed", "stopped"}


class Checks:
    def __init__(self):
        self.failures = 0

    def ok(self, cond, msg):
        mark = "\033[32mPASS\033[0m" if cond else "\033[31mFAIL\033[0m"
        print(f"  {mark} {msg}", flush=True)
        if not cond:
            self.failures += 1
        return cond


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    expect_command_ack = "--expect-command-ack" in sys.argv
    if not args:
        print("usage: assert_boundary.py <project_id> [--expect-command-ack]",
              file=sys.stderr)
        return 2
    pid = args[0]

    s = get_settings()
    c = Checks()
    print(f"Asserting boundary for project {pid}")

    with db.get_session(s.db_path) as sess:
        p = db.get_project(sess, pid)
        c.ok(p is not None, "project row exists")
        if p is not None:
            # 1. status: reached a terminal state (report_status round-tripped).
            c.ok((p.status or "") in TERMINAL,
                 f"run reached terminal status (status={p.status!r})")

            # 2. events: live-log lines were pushed over /v1/.../events.
            evs = db.list_events(sess, pid, after_id=0)
            c.ok(len(evs) > 0, f"live-log events stored over the boundary ({len(evs)} lines)")

        # 3. command pull + ack + apply (loop mode drives checkpoint()).
        if expect_command_ack:
            pending = db.list_pending_commands(sess, pid)
            c.ok(len(pending) == 0,
                 f"seeded command was pulled + acked (0 pending, was 1)")
            p2 = db.get_project(sess, pid)
            c.ok((getattr(p2, "autonomy_level", "") or "") == SEED_AUTONOMY,
                 f"set_autonomy applied (autonomy_level={getattr(p2, 'autonomy_level', None)!r})")
        else:
            print("  (skipping command-ack check — apply mode does not poll control)",
                  flush=True)

    # 4. auth: a token for a DIFFERENT project must be 403 on this project's route.
    base = (s.control_plane_url or "").rstrip("/")
    if base:
        other = make_job_token("some-other-project", s.secret_key, ttl_seconds=300)
        req = urllib.request.Request(f"{base}/projects/{pid}", method="GET")
        req.add_header("Authorization", f"Bearer {other}")
        try:
            urllib.request.urlopen(req, timeout=5)
            c.ok(False, "wrong-project token rejected (got 200, expected 403)")
        except urllib.error.HTTPError as e:
            c.ok(e.code == 403, f"wrong-project token rejected over /v1 ({e.code})")
        except Exception as e:
            c.ok(False, f"wrong-project token check errored: {e}")
    else:
        print("  (skipping /v1 auth check — CONTROL_PLANE_URL not set in webapp)",
              flush=True)

    if c.failures:
        print(f"\n\033[31m{c.failures} boundary check(s) FAILED\033[0m")
        return 1
    print("\n\033[32mAll boundary checks passed.\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
