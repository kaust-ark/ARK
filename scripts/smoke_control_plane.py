#!/usr/bin/env python
"""Smoke-test the /v1 control-plane boundary against a RUNNING webapp.

Exercises the pieces of Phase 1 that are otherwise only compile-checked — the /v1
API, token auth, the HttpControlPlaneClient, event storage + the dashboard log
read, and the HITL decision flow — WITHOUT needing OpenHands, an LLM key, or a
real research run. It plays the role the orchestrator would over HTTP.

Prereq: the webapp is running with CONTROL_PLANE_URL set, e.g.
    CONTROL_PLANE_URL=http://127.0.0.1:9527/v1  in ~/.config/ARK/webapp.env
Then, in the same env (conda ark / venv with .[webapp]):
    python scripts/smoke_control_plane.py

It creates a throwaway project row, mints a job token, drives the boundary as the
orchestrator would, simulates a Telegram answer via the HITL engine, and prints a
dashboard URL so you can eyeball the live logs + decision in the browser.
"""

import sys
import time

# Import the SAME modules the webapp uses (must run in the webapp's env).
from website.dashboard.config import get_settings
from website.dashboard import db, hitl
from website.dashboard.auth import make_job_token
from ark.controlplane import HttpControlPlaneClient


def _ok(msg):   print(f"  \033[32mPASS\033[0m {msg}")
def _fail(msg): print(f"  \033[31mFAIL\033[0m {msg}")


def main() -> int:
    s = get_settings()
    base = (s.control_plane_url or "").strip()
    if not base:
        print("CONTROL_PLANE_URL is not set in webapp.env — set it (e.g. "
              "http://127.0.0.1:9527/v1) and restart the webapp first.")
        return 2
    print(f"Control plane: {base}")
    print(f"DB: {s.db_path}\n")

    # 1. Seed a throwaway user + project directly in the DB (as the webapp would).
    with db.get_session(s.db_path) as sess:
        user, _ = db.get_or_create_user_by_email(sess, "smoke@example.com")
        project = db.create_project(sess, user_id=user.id,
                                    name=f"smoke-{int(time.time())}",
                                    status="running")
        pid, uid = project.id, user.id
    print(f"Seeded project {pid}\n")

    # 2. Mint the per-run token the launcher would, and build the HTTP client the
    #    orchestrator would — this is the real boundary from here on.
    token = make_job_token(pid, s.secret_key)
    cp = HttpControlPlaneClient(base, token, pid)
    failures = 0

    # 3. Bootstrap + status report (server-side update via /v1).
    view = cp.fetch_project()
    (_ok if view and view.id == pid else _fail)("fetch_project() over /v1")
    failures += 0 if (view and view.id == pid) else 1

    cp.report_status(status="running", phase="review", score=7.25, iteration=3)
    with db.get_session(s.db_path) as sess:
        p = db.get_project(sess, pid)
    good = p.phase == "review" and abs(p.score - 7.25) < 1e-6 and p.iteration == 3
    (_ok if good else _fail)("report_status() persisted (phase/score/iteration)")
    failures += 0 if good else 1

    # 4. Live logs: push lines and confirm they land in the event store (what the
    #    dashboard /log + /stream now read for HTTP-mode projects).
    cp.append_events([{"ts": "t", "line": f"[smoke] log line {i}"} for i in range(5)])
    with db.get_session(s.db_path) as sess:
        evs = db.list_events(sess, pid, after_id=0)
    good = len(evs) >= 5 and evs[-1]["line"].startswith("[smoke] log line")
    (_ok if good else _fail)(f"append_events() stored ({len(evs)} lines)")
    failures += 0 if good else 1

    # 5. Auth: a token scoped to a DIFFERENT project must be rejected (403).
    import urllib.request, urllib.error
    other = make_job_token("some-other-project", s.secret_key)
    req = urllib.request.Request(f"{base}/projects/{pid}", method="GET")
    req.add_header("Authorization", f"Bearer {other}")
    try:
        urllib.request.urlopen(req, timeout=5)
        _fail("wrong-project token was NOT rejected"); failures += 1
    except urllib.error.HTTPError as e:
        (_ok if e.code == 403 else _fail)(f"wrong-project token rejected ({e.code})")
        failures += 0 if e.code == 403 else 1

    # 6. HITL decision: open it over /v1, simulate a Telegram answer via the CP
    #    HITL engine, then confirm the orchestrator's poll sees it answered.
    did = cp.open_decision("Smoke: proceed?", ["Yes", "No"], default_index=1,
                           context="smoke-test decision")
    (_ok if did else _fail)("open_decision() over /v1")
    failures += 0 if did else 1
    if did:
        dv = cp.get_decision(did)
        good = dv is not None and dv.status == "pending"
        (_ok if good else _fail)("get_decision() shows pending")
        failures += 0 if good else 1

        # Simulate the inbound Telegram reply the daemon would capture.
        with db.get_session(s.db_path) as sess:
            answered = hitl.apply_reply(sess, pid, "1")
        (_ok if answered else _fail)("hitl.apply_reply() answered the open decision")
        failures += 0 if answered else 1

        dv = cp.get_decision(did)
        good = dv is not None and dv.status == "answered" and dv.answer_index == 0
        (_ok if good else _fail)("orchestrator poll sees answered (index 0)")
        failures += 0 if good else 1

    # 7. Timeout sweep is a no-op here (no past deadline) but must not raise.
    with db.get_session(s.db_path) as sess:
        hitl.sweep(sess)
    _ok("hitl.sweep() ran")

    prefix = "/dashboard"
    print(f"\nEyeball it: {s.base_url}{prefix}/#project/{pid}")
    print("  → the log lines above should render live; the decision should show answered.\n")

    if failures:
        print(f"\033[31m{failures} check(s) FAILED\033[0m")
        return 1
    print("\033[32mAll boundary checks passed.\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
