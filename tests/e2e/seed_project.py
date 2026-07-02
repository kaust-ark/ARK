#!/usr/bin/env python3
"""Seed a throwaway project + mint a job token for the container e2e.

Runs INSIDE the webapp (control-plane) container, where the SQLite DB and
SECRET_KEY live. It plays the role the webapp normally plays when a user starts
a run: create the project row, enqueue a (non-disruptive) control command so the
run has something to pull+ack over the boundary, and mint the per-run scoped
bearer token the orchestrator will present on every /v1 call.

Emits two machine-parseable lines on stdout for the runner to capture:
    PROJECT_ID=<uuid>
    JOB_TOKEN=<token>
Everything else goes to stderr.
"""

import sys
import time

from website.dashboard.config import get_settings
from website.dashboard import db
from website.dashboard.auth import make_job_token

# The autonomy the seeded set_autonomy command flips the run to. "full_auto" is
# a recognized, least-gating level (see Orchestrator._AUTONOMY_ASK) — distinct
# from the "collaborative" default so the assert can observe the change, without
# pushing the run toward blocking on decisions.
SEED_AUTONOMY = "full_auto"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def main() -> int:
    s = get_settings()
    log(f"[seed] DB: {s.db_path}")

    with db.get_session(s.db_path) as sess:
        user, _ = db.get_or_create_user_by_email(sess, "e2e@example.com")
        project = db.create_project(sess, user_id=user.id,
                                    name=f"e2e-{int(time.time())}",
                                    status="queued")
        pid = project.id
        # A non-disruptive command the orchestrator will peek + apply + ack.
        db.enqueue_command(sess, pid, "set_autonomy", payload=SEED_AUTONOMY)

    token = make_job_token(pid, s.secret_key, ttl_seconds=3600)

    log(f"[seed] seeded project {pid} with a set_autonomy={SEED_AUTONOMY} command")
    # Machine-parseable — the runner greps these two lines.
    print(f"PROJECT_ID={pid}")
    print(f"JOB_TOKEN={token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
