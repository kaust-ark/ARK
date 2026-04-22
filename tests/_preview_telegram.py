"""Print the exact Telegram messages ARK would send, end-to-end.

Run directly: `python tests/_preview_telegram.py`

This is a preview harness, not a pytest — it gives the developer eyes
on the wire format so they can spot copy/format issues the unit tests
can't catch. Nothing is actually sent over the network; the Telegram
API is stubbed at urllib.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import types
import yaml

os.environ["ARK_TELEGRAM_BOT_TOKEN"] = "fake_token"
os.environ["ARK_TELEGRAM_CHAT_ID"] = "12345"


# ── Fake urlopen that captures outgoing sendMessage calls ─────────────
SENT: list[dict] = []


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body


def _fake_urlopen(request, timeout=None, context=None):
    try:
        body = json.loads(request.data or b"{}")
    except Exception:
        body = {}
    if "sendMessage" in request.full_url:
        SENT.append(body)
    if "sendChatAction" in request.full_url:
        pass
    if "getUpdates" in request.full_url:
        return _Resp({"ok": True, "result": []})
    return _Resp({"ok": True, "result": {"message_id": len(SENT)}})


import urllib.request
urllib.request.urlopen = _fake_urlopen


# ── Tag→plain-text renderer so we see what Telegram would display ─────

def render(msg: dict) -> str:
    text = msg.get("text", "")
    # Strip a subset of Telegram HTML so the preview reads naturally.
    text = re.sub(r"</?b>", "", text)
    text = re.sub(r"</?i>", "", text)
    text = re.sub(r"</?code>", "`", text)
    text = re.sub(r"</?pre>", "\n", text)
    text = re.sub(r'<a href="[^"]+">([^<]+)</a>', r"\1", text)
    return html.unescape(text)


# ── Stub orchestrator ─────────────────────────────────────────────────

from ark.orchestrator import Orchestrator
from ark.telegram import TelegramDispatcher, TelegramConfig


def make_orch(project_id, title=None, iteration=0, score=0.0, mode="paper"):
    orch = Orchestrator.__new__(Orchestrator)
    tmp = Path("/tmp/ark_preview")
    tmp.mkdir(exist_ok=True)
    state = tmp / "state"
    state.mkdir(exist_ok=True)
    logs = tmp / "logs"
    logs.mkdir(exist_ok=True)
    log = logs / f"{project_id}.log"
    log.write_text("")

    orch.project_name = project_id
    orch._project_id = project_id
    orch._display_name = None
    orch.config = {"title": title} if title else {}
    orch.mode = mode
    orch.iteration = iteration
    orch.max_iterations = 10
    orch.paper_accept_threshold = 8
    orch.state_dir = state
    orch.log_dir = logs
    orch.log_file = log
    orch.paper_state_file = state / "paper_state.yaml"
    orch.code_dir = tmp
    orch._db_path = None
    orch._tg_chat_history = []
    orch._tg_chat_lock = threading.Lock()
    orch._artifact_threads = []
    orch._artifact_threads_lock = threading.Lock()
    orch.memory = types.SimpleNamespace(stagnation_count=0, scores=[])
    orch._last_score = score
    orch.telegram = TelegramDispatcher(
        project_id, TelegramConfig(project_config=orch.config)
    )
    return orch


def divider(label):
    print()
    print("═" * 70)
    print(f"  {label}")
    print("═" * 70)


UUID = "d9b7fab8-b466-40ba-978c-2fe464dae9bc"


# ── Preview 1: session start, no title yet ────────────────────────────
SENT.clear()
orch = make_orch(UUID)  # no title → should fall back to Project-d9b7f
try:
    orch._send_session_banner()
except AttributeError:
    # legacy name
    orch.send_session_start_banner()
divider("① Session start (no title yet — first run)")
for m in SENT:
    print(render(m))


# ── Preview 2: after title generated ──────────────────────────────────
SENT.clear()
orch = make_orch(UUID, title="A Novel Approach to Research Automation")
orch.telegram.start()
orch.notify_progress("Title generated", "A Novel Approach to Research Automation",
                     level="done")
time.sleep(0.5)
orch.telegram.stop()
divider("② Title generated progress ping")
for m in SENT:
    print(render(m))


# ── Preview 3: env setup ──────────────────────────────────────────────
SENT.clear()
orch = make_orch(UUID, title="A Novel Approach to Research Automation")
orch.telegram.start()
orch.notify_progress("Env setup", "cloning base env ark-base...", level="working")
orch.notify_progress("Env ready", "conda env 'myproj' created", level="done")
time.sleep(0.5)
orch.telegram.stop()
divider("③ Env provisioning")
for m in SENT:
    print(render(m))
    print()


# ── Preview 4: dev phase messages ─────────────────────────────────────
SENT.clear()
orch = make_orch(UUID, title="A Novel Approach to Research Automation", mode="dev")
from ark.pipeline import PipelineMixin
for event, cur, total in [("start", 0, 5), ("iteration", 1, 5),
                          ("experiments", 1, 5), ("writing", 1, 5),
                          ("complete", 5, 5)]:
    orch._send_dev_phase_telegram(event, cur, total)
divider("④ Dev Phase events")
for m in SENT:
    print(render(m))
    print()


# ── Preview 5: decision request ───────────────────────────────────────
SENT.clear()
orch = make_orch(UUID, title="A Novel Approach to Research Automation",
                 iteration=3, score=6.5)
orch.telegram.start()
result_holder = {}


def _call():
    result_holder["v"] = orch.ask_user_decision(
        question="Experiments failed",
        options=["Retry now", "Skip and continue"],
        timeout=1,
        default=0,
        what_happened="Step 3 raised ImportError: No module named 'torch'",
        background=[
            "Phase: experiments",
            "Iteration: 3",
            "Score has been stalled at 6.5/10 for 2 rounds",
        ],
        option_details=[
            "Re-runs the failing step in the same environment.",
            "Marks this step as done with broken output and moves on.",
        ],
        polish=False,
    )


t = threading.Thread(target=_call, daemon=True)
t.start()
t.join(timeout=3)
orch.telegram.stop()
divider("⑤ Decision request (what the user sees when ARK asks)")
for m in SENT:
    if "Decision" in m.get("text", "") or "Experiments failed" in m.get("text", ""):
        print(render(m))


# ── Preview 6: iteration summary ──────────────────────────────────────
SENT.clear()
orch = make_orch(UUID, title="A Novel Approach to Research Automation",
                 iteration=4, score=7.2)


def _send(text, parse_mode=""):
    SENT.append({"text": text, "parse_mode": parse_mode})


orch.telegram.send_async = lambda text, parse_mode="", polish=False, polish_ctx=None: _send(text, parse_mode)  # noqa: ARG005
orch.send_iteration_summary(score=7.2, prev_score=6.5, review_text="")
divider("⑥ Iteration summary")
for m in SENT:
    print(render(m))
    print()


# ── Preview 7: stopped-project scenario (daemon agent reply) ──────────
divider("⑦ Incoming routing (daemon bind/release)")
from ark.telegram_daemon import TelegramDaemon

class _DaemonCapture(TelegramDaemon):
    def __init__(self, state_dir):
        self._ark_dir = state_dir
        self._pid_file = state_dir / "pid"
        self._state_file = state_dir / "telegram_state.yaml"
        self._lock_file = state_dir / "lock"
        self._state_lock_file = state_dir / ".lock"
        self._mailbox_dir = state_dir / "mailbox"
        self._mailbox_dir.mkdir(parents=True, exist_ok=True)
        self._config = TelegramConfig()
        self._stop_event = threading.Event()
        self._lock_fd = None
        self._offset = 0
        self.outbox = []

    def _api_call(self, method, **p):
        self.outbox.append({"method": method, **p})
        if method == "getUpdates":
            return {"ok": True, "result": []}
        return {"ok": True, "result": True}


dtmp = Path("/tmp/ark_preview/daemon")
import shutil
if dtmp.exists():
    shutil.rmtree(dtmp)
dtmp.mkdir(parents=True)
daemon = _DaemonCapture(dtmp)
daemon._save_state({
    "registered_projects": ["safeclaw", "neuralnet"],
    "active_projects": {"safeclaw": {"pid": 99999}, "neuralnet": {"pid": 99998}},
})

print("\n--- User types: /bind safeclaw ---")
daemon._handle_bind_command("/bind safeclaw")
for m in daemon.outbox[-1:]:
    print(render(m))

print("\n--- User types: /bound ---")
daemon.outbox.clear()
daemon._handle_bind_command("/bound")
for m in daemon.outbox[-1:]:
    print(render(m))

print("\n--- User types: /release ---")
daemon.outbox.clear()
daemon._handle_bind_command("/release")
for m in daemon.outbox[-1:]:
    print(render(m))

print("\n--- Bound, user types 'pdf please' → routed to safeclaw (no ask) ---")
daemon._save_state({
    "registered_projects": ["safeclaw", "neuralnet"],
    "active_projects": {"safeclaw": {"pid": 99999}, "neuralnet": {"pid": 99998}},
    "bound_project": "safeclaw",
})
delivered = []
daemon._deliver_to_mailbox = lambda p, t, u: delivered.append((p, t))
daemon._prune_dead = lambda s: None
daemon._route_message("pdf please", update_id=42)
print(f"→ delivered to mailbox: {delivered}")

print("\n--- Prefix override: 'neuralnet status' bypasses bind ---")
delivered.clear()
daemon._route_message("neuralnet status", update_id=43)
print(f"→ delivered to mailbox: {delivered}")

print()
print("═" * 70)
print("  Preview complete")
print("═" * 70)
