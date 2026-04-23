"""End-to-end validation of the Telegram communication framework.

Covers both sides:
  * Sending — unified header, `Project-<id5>` fallback, fine-grained progress,
    dev phase, research phase, decision format.
  * Receiving — daemon routing with /bind, /release, /bound;
    agent [ACTION]/[INSTRUCTION]/[SEND_PDF] handling;
    side effects on user_updates.yaml + user_instructions.yaml.

The Telegram API is stubbed at urllib.request.urlopen so the tests run
offline. The Claude CLI (used by the stopped-project agent) is stubbed at
subprocess.run so we can feed canned responses and check tag parsing.
"""

from __future__ import annotations

import io
import json
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ══════════════════════════════════════════════════════════════
#  Fake Telegram API
# ══════════════════════════════════════════════════════════════


class _FakeTelegramServer:
    """Captures outgoing Telegram sendMessage calls and hands back fake
    getUpdates payloads. Shared across a single test via `monkeypatch`
    of urllib.request.urlopen.
    """

    def __init__(self):
        self.sent: list[dict] = []
        self.pending_updates: list[dict] = []

    def add_update(self, text: str, update_id: int = None):
        if update_id is None:
            update_id = 1_000_000 + len(self.pending_updates)
        self.pending_updates.append({
            "update_id": update_id,
            "message": {"text": text, "chat": {"id": 1}, "from": {"id": 2}},
        })

    def handle(self, request, timeout=None, context=None):
        url = request.full_url
        body = request.data or b""
        # Multipart uploads don't carry JSON bodies; they're always
        # sendDocument. Record the bare fact and move on.
        if "sendDocument" in url:
            self.sent.append({"method": "sendDocument", "url": url})
            return _FakeResponse({"ok": True, "result": {"document": {"file_size": 1}}})

        try:
            params = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            params = {}

        if "sendMessage" in url:
            self.sent.append({"method": "sendMessage", **params})
            return _FakeResponse({"ok": True, "result": {"message_id": len(self.sent)}})

        if "sendChatAction" in url:
            return _FakeResponse({"ok": True, "result": True})

        if "getUpdates" in url:
            offset = params.get("offset", 0)
            # `offset=-1` is the startup baseline fetch; return empty so
            # the caller's baseline advances past our queued updates.
            if offset == -1:
                return _FakeResponse({"ok": True, "result": []})
            batch = [u for u in self.pending_updates if u["update_id"] >= offset]
            self.pending_updates = [u for u in self.pending_updates if u["update_id"] >= offset + len(batch)]
            return _FakeResponse({"ok": True, "result": batch})

        return _FakeResponse({"ok": True, "result": []})


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def fake_telegram(monkeypatch):
    """Replaces urllib.request.urlopen so all Telegram API calls are captured."""
    fake = _FakeTelegramServer()

    def _fake_urlopen(request, timeout=None, context=None):
        return fake.handle(request, timeout=timeout, context=context)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setenv("ARK_TELEGRAM_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("ARK_TELEGRAM_CHAT_ID", "12345")
    return fake


# ══════════════════════════════════════════════════════════════
#  Stub Orchestrator
# ══════════════════════════════════════════════════════════════


def _mk_orchestrator(tmp_path, project_id, project_name=None, title=None,
                     config_extra=None):
    """Build a minimal Orchestrator without running the heavy ctor.

    The real ctor chdirs, loads agents, and touches the DB. We only need
    the methods under test, so we bypass it and wire up the attributes by
    hand. Keeps the test fast and hermetic.
    """
    from ark.orchestrator import Orchestrator
    from ark.telegram import TelegramDispatcher, TelegramConfig

    if project_name is None:
        project_name = project_id

    state_dir = tmp_path / "auto_research" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log_dir = tmp_path / "auto_research" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{project_name}_run.log"
    log_file.write_text("")

    orch = Orchestrator.__new__(Orchestrator)
    orch.project_name = project_name
    orch._project_id = project_id
    orch._display_name = None
    orch.config = {"title": title} if title else {}
    if config_extra:
        orch.config.update(config_extra)
    orch.mode = "paper"
    orch.iteration = 0
    orch.max_iterations = 10
    orch.paper_accept_threshold = 8
    orch.state_dir = state_dir
    orch.log_dir = log_dir
    orch.log_file = log_file
    orch.code_dir = tmp_path
    orch._db_path = None
    orch._tg_chat_history = []
    orch._tg_chat_lock = threading.Lock()
    orch._artifact_threads = []
    orch._artifact_threads_lock = threading.Lock()
    # Minimal memory stub
    orch.memory = types.SimpleNamespace(stagnation_count=0, scores=[])
    orch._last_score = 0
    # Real dispatcher — it's cheap to construct and exercises the same
    # send path the production code uses.
    orch.telegram = TelegramDispatcher(
        project_name, TelegramConfig(project_config=orch.config)
    )
    return orch


# ══════════════════════════════════════════════════════════════
#  Sending: header & display_name
# ══════════════════════════════════════════════════════════════


class TestHeader:
    def test_short_id_from_uuid(self, tmp_path):
        uuid_id = "d9b7fab8-b466-40ba-978c-2fe464dae9bc"
        orch = _mk_orchestrator(tmp_path, project_id=uuid_id, project_name=uuid_id)
        assert orch.short_id == "d9b7f"

    def test_display_name_falls_back_to_project_id5_when_title_is_uuid(self, tmp_path):
        uuid_id = "d9b7fab8-b466-40ba-978c-2fe464dae9bc"
        orch = _mk_orchestrator(
            tmp_path, project_id=uuid_id, project_name=uuid_id,
            title=uuid_id,  # placeholder title — still looks like a UUID
        )
        assert orch.display_name == "Project-d9b7f"

    def test_display_name_uses_real_title_when_set(self, tmp_path):
        uuid_id = "d9b7fab8-b466-40ba-978c-2fe464dae9bc"
        orch = _mk_orchestrator(
            tmp_path, project_id=uuid_id, project_name=uuid_id,
            title="Attention Is All You Need",
        )
        assert orch.display_name == "Attention Is All You Need"

    def test_header_contains_short_id_and_title(self, tmp_path):
        uuid_id = "d9b7fab8-b466-40ba-978c-2fe464dae9bc"
        orch = _mk_orchestrator(
            tmp_path, project_id=uuid_id, project_name=uuid_id,
            title="My Paper",
        )
        header = orch.tg_header("🚤")
        assert "ARK Project-d9b7f" in header
        assert "My Paper" in header
        assert header.startswith("🚤 ")

    def test_header_without_title_shows_only_project_id(self, tmp_path):
        uuid_id = "d9b7fab8-b466-40ba-978c-2fe464dae9bc"
        orch = _mk_orchestrator(tmp_path, project_id=uuid_id, project_name=uuid_id)
        header = orch.tg_header("🚤")
        assert "ARK Project-d9b7f" in header
        assert "|" not in header

    def test_invalidate_display_name_repicks_up_title(self, tmp_path):
        uuid_id = "d9b7fab8-b466-40ba-978c-2fe464dae9bc"
        orch = _mk_orchestrator(tmp_path, project_id=uuid_id, project_name=uuid_id)
        _ = orch.display_name  # prime the cache → Project-d9b7f
        orch.config["title"] = "Fresh Title"
        orch._invalidate_display_name()
        assert orch.display_name == "Fresh Title"

    def test_looks_like_uuid_detects_bare_hex(self, tmp_path):
        from ark.orchestrator import Orchestrator
        assert Orchestrator._looks_like_uuid("d9b7fab8-b466-40ba-978c-2fe464dae9bc")
        assert not Orchestrator._looks_like_uuid("safeclaw-v2")
        assert not Orchestrator._looks_like_uuid("Attention Is All You Need")
        assert not Orchestrator._looks_like_uuid("")


# ══════════════════════════════════════════════════════════════
#  Sending: message format captured over fake Telegram
# ══════════════════════════════════════════════════════════════


class TestSentMessages:
    def test_status_block_carries_header_as_first_line(self, tmp_path, fake_telegram):
        orch = _mk_orchestrator(
            tmp_path,
            project_id="d9b7fab8-b466-40ba-978c-2fe464dae9bc",
            title="Attention Is All You Need",
        )
        block = orch._status_block()
        first_line = block.split("\n", 1)[0]
        assert "ARK Project-d9b7f" in first_line
        assert "Attention Is All You Need" in first_line

    def test_notify_progress_includes_header_and_stage(self, tmp_path, fake_telegram):
        orch = _mk_orchestrator(
            tmp_path,
            project_id="d9b7fab8-b466-40ba-978c-2fe464dae9bc",
            title="Neural Paper",
        )
        orch.telegram.start()
        try:
            orch.notify_progress("Title generated", "My Great Paper", level="done")
            # Drain: sender thread pulls from the queue and POSTs — wait
            # until the message actually lands on our fake server.
            import time
            deadline = time.time() + 3
            while time.time() < deadline and not fake_telegram.sent:
                time.sleep(0.05)
        finally:
            orch.telegram.stop()

        payloads = [m for m in fake_telegram.sent if m.get("method") == "sendMessage"]
        assert payloads, "notify_progress did not send"
        body = payloads[-1]["text"]
        assert "ARK Project-d9b7f" in body
        assert "Neural Paper" in body
        assert "Title generated" in body
        assert "My Great Paper" in body

    def test_notify_progress_respects_opt_out(self, tmp_path, fake_telegram):
        orch = _mk_orchestrator(
            tmp_path,
            project_id="abc12345-b466-40ba-978c-2fe464dae9bc",
            config_extra={"telegram_progress_notify": False},
        )
        orch.telegram.start()
        try:
            orch.notify_progress("noisy", "stage", level="info")
        finally:
            orch.telegram.stop()
        payloads = [m for m in fake_telegram.sent if m.get("method") == "sendMessage"]
        assert not payloads, "progress should have been suppressed by config flag"

    def test_decision_message_has_header_and_timeout_hint(self, tmp_path, fake_telegram):
        orch = _mk_orchestrator(
            tmp_path,
            project_id="d9b7fab8-b466-40ba-978c-2fe464dae9bc",
            title="Some Paper",
        )
        orch.telegram.start()
        # Run ask_user_decision on a thread and short-circuit the wait by
        # reading the sent message directly — we don't need a real reply
        # to validate the format.
        result_holder = {}

        def _call():
            result_holder["value"] = orch.ask_user_decision(
                question="Experiments failed",
                options=["Retry", "Skip"],
                timeout=1,  # trigger timeout quickly
                default=0,
                what_happened="Step 3 raised ImportError",
                background=["Phase: experiments", "Iteration: 2"],
                option_details=[
                    "Re-run the failing step",
                    "Mark broken output as done and continue",
                ],
                polish=False,
            )

        t = threading.Thread(target=_call, daemon=True)
        t.start()
        t.join(timeout=5)
        orch.telegram.stop()

        payloads = [m for m in fake_telegram.sent if m.get("method") == "sendMessage"]
        assert payloads, "ask_user_decision did not send"
        body = payloads[0]["text"]
        assert "ARK Project-d9b7f" in body
        assert "Some Paper" in body
        assert "Decision needed" in body
        assert "Experiments failed" in body or "Step 3 raised ImportError" in body
        assert "Retry" in body
        assert "Custom" in body  # escape option auto-appended
        # Default timeout in seconds (timeout=1 → 0 min rounds up to 1)
        assert "min" in body

    def test_decision_default_timeout_is_ten_minutes(self, tmp_path, fake_telegram):
        """Regression: user asked for 10-min default, not 15."""
        from ark.orchestrator import Orchestrator
        import inspect
        sig = inspect.signature(Orchestrator.ask_user_decision)
        assert sig.parameters["timeout"].default == 600


# ══════════════════════════════════════════════════════════════
#  Receiving: daemon bind commands + routing
# ══════════════════════════════════════════════════════════════


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    """A TelegramDaemon with its state dir redirected into tmp_path."""
    from ark.telegram_daemon import TelegramDaemon

    ark_dir = tmp_path / ".ark"
    ark_dir.mkdir()
    monkeypatch.setattr("ark.telegram_daemon.get_config_dir", lambda: ark_dir)
    # Stub out the bot config so _send/_api_call are "configured"
    monkeypatch.setenv("ARK_TELEGRAM_BOT_TOKEN", "fake_token")
    monkeypatch.setenv("ARK_TELEGRAM_CHAT_ID", "12345")

    # Capture sendMessage calls
    sent: list[dict] = []

    def _fake_api_call(method, **params):
        sent.append({"method": method, **params})
        if method == "getUpdates":
            return {"ok": True, "result": []}
        return {"ok": True, "result": True}

    d = TelegramDaemon()
    d._api_call = _fake_api_call  # type: ignore[assignment]
    d.sent = sent  # type: ignore[attr-defined]
    return d


class TestDaemonBind:
    def test_bind_requires_registered_project(self, daemon):
        daemon._save_state({"registered_projects": ["alpha", "beta"]})
        assert daemon._handle_bind_command("/bind gamma")
        reply = daemon.sent[-1]["text"]
        assert "not registered" in reply
        state = daemon._load_state()
        assert state.get("bound_project") in (None, "")  # unchanged

    def test_bind_sets_state_and_confirms(self, daemon):
        daemon._save_state({
            "registered_projects": ["alpha", "beta"],
            "active_projects": {"alpha": {"pid": 1}},
        })
        assert daemon._handle_bind_command("/bind alpha")
        state = daemon._load_state()
        assert state["bound_project"] == "alpha"
        reply = daemon.sent[-1]["text"]
        assert "Bound to" in reply and "alpha" in reply

    def test_bound_reports_current_binding(self, daemon):
        daemon._save_state({
            "registered_projects": ["alpha"],
            "bound_project": "alpha",
            "active_projects": {"alpha": {"pid": 1}},
        })
        assert daemon._handle_bind_command("/bound")
        reply = daemon.sent[-1]["text"]
        assert "alpha" in reply

    def test_bound_reports_none_when_unset(self, daemon):
        daemon._save_state({"registered_projects": ["alpha"]})
        assert daemon._handle_bind_command("/bound")
        reply = daemon.sent[-1]["text"]
        assert "No project" in reply

    def test_release_clears_binding(self, daemon):
        daemon._save_state({
            "registered_projects": ["alpha"],
            "bound_project": "alpha",
        })
        assert daemon._handle_bind_command("/release")
        state = daemon._load_state()
        assert state.get("bound_project") is None

    def test_release_when_not_bound_explains(self, daemon):
        daemon._save_state({"registered_projects": ["alpha"]})
        assert daemon._handle_bind_command("/release")
        reply = daemon.sent[-1]["text"]
        assert "No project" in reply


class TestDaemonRouting:
    def test_bound_project_beats_heuristics(self, daemon, monkeypatch):
        """If alpha is bound, a bare 'status' message must go to alpha even
        though beta is also active. Heuristics like last_sender no longer win.
        """
        daemon._save_state({
            "registered_projects": ["alpha", "beta"],
            "active_projects": {
                "alpha": {"pid": 99999},
                "beta": {"pid": 99998},
            },
            "bound_project": "alpha",
            "last_sender": "beta",
        })
        # Make _prune_dead a no-op so our fake PIDs survive
        monkeypatch.setattr(daemon, "_prune_dead", lambda s: None)

        delivered = []

        def _fake_deliver(project, text, update_id):
            delivered.append((project, text))

        daemon._deliver_to_mailbox = _fake_deliver  # type: ignore[assignment]
        daemon._route_message("how's it going", update_id=1)

        assert delivered == [("alpha", "how's it going")]

    def test_explicit_prefix_still_overrides_bind(self, daemon, monkeypatch):
        """Binding pins the default, but prefix routing still lets the user
        poke another project with one message.
        """
        daemon._save_state({
            "registered_projects": ["alpha", "beta"],
            "active_projects": {
                "alpha": {"pid": 99999},
                "beta": {"pid": 99998},
            },
            "bound_project": "alpha",
        })
        monkeypatch.setattr(daemon, "_prune_dead", lambda s: None)
        delivered = []
        daemon._deliver_to_mailbox = (
            lambda p, t, u: delivered.append((p, t))
        )  # type: ignore[assignment]

        daemon._route_message("beta pdf please", update_id=2)
        assert delivered == [("beta", "pdf please")]

    def test_stale_bound_project_is_cleared(self, daemon, monkeypatch):
        daemon._save_state({
            "registered_projects": ["alpha"],
            "bound_project": "deleted_project",
            "active_projects": {"alpha": {"pid": 99999}},
        })
        monkeypatch.setattr(daemon, "_prune_dead", lambda s: None)
        delivered = []
        daemon._deliver_to_mailbox = (
            lambda p, t, u: delivered.append((p, t))
        )  # type: ignore[assignment]

        daemon._route_message("hi", update_id=3)
        state = daemon._load_state()
        assert state.get("bound_project") is None
        # Single-project fallback should kick in
        assert delivered and delivered[0][0] == "alpha"


# ══════════════════════════════════════════════════════════════
#  Agent directives: [ACTION] / [INSTRUCTION] → yaml
# ══════════════════════════════════════════════════════════════


class TestAgentDirectives:
    def _run(self, orch, canned_response):
        """Run _agent_respond_telegram with a mocked claude CLI."""
        class _Proc:
            stdout = canned_response
            stderr = ""
            returncode = 0

        with patch("subprocess.run", return_value=_Proc):
            orch._agent_respond_telegram("user message")

    def test_action_tag_writes_user_updates(self, tmp_path, fake_telegram):
        orch = _mk_orchestrator(
            tmp_path,
            project_id="abcdef12-b466-40ba-978c-2fe464dae9bc",
            title="Test",
        )
        # `inject_user_update` lives on the orchestrator — make sure it
        # writes to state_dir/user_updates.yaml by default.
        orch.inject_user_update = lambda msg: _inject(orch, msg)

        self._run(
            orch,
            "Sure, I'll skip experiments this round.\n[ACTION: skip experiments this iteration]",
        )

        updates_file = orch.state_dir / "user_updates.yaml"
        assert updates_file.exists(), "ACTION should write user_updates.yaml"
        data = yaml.safe_load(updates_file.read_text())
        messages = [u["message"] for u in data.get("updates", [])]
        assert any("skip experiments" in m for m in messages)

    def test_instruction_tag_writes_user_instructions(self, tmp_path, fake_telegram):
        orch = _mk_orchestrator(
            tmp_path,
            project_id="abcdef12-b466-40ba-978c-2fe464dae9bc",
            title="Test",
        )
        orch.inject_user_update = lambda msg: _inject(orch, msg)

        self._run(
            orch,
            "Noted.\n[INSTRUCTION: always use PyTorch, never TensorFlow]",
        )

        instr_file = orch.state_dir / "user_instructions.yaml"
        assert instr_file.exists(), "INSTRUCTION should write user_instructions.yaml"
        data = yaml.safe_load(instr_file.read_text())
        messages = [u["message"] for u in data.get("instructions", [])]
        assert any("PyTorch" in m for m in messages)

    def test_plain_reply_has_no_side_effects(self, tmp_path, fake_telegram):
        orch = _mk_orchestrator(
            tmp_path,
            project_id="abcdef12-b466-40ba-978c-2fe464dae9bc",
            title="Test",
        )
        orch.inject_user_update = lambda msg: _inject(orch, msg)

        self._run(orch, "All good! Everything is on track.")

        assert not (orch.state_dir / "user_updates.yaml").exists()
        assert not (orch.state_dir / "user_instructions.yaml").exists()

    def test_system_prompt_advertises_capabilities(self, tmp_path):
        orch = _mk_orchestrator(
            tmp_path,
            project_id="abcdef12-b466-40ba-978c-2fe464dae9bc",
            title="Test",
        )
        prompt = orch._build_tg_system_prompt()
        assert "Project-abcde" in prompt
        assert "[SEND_PDF]" in prompt
        assert "[ACTION:" in prompt
        assert "[INSTRUCTION:" in prompt
        # Guardrails are present
        assert "destructive" in prompt.lower() or "confirm" in prompt.lower()


def _inject(orch, msg: str):
    """Mirror the orchestrator's real inject_user_update file format."""
    updates_file = orch.state_dir / "user_updates.yaml"
    data = {}
    if updates_file.exists():
        data = yaml.safe_load(updates_file.read_text()) or {}
    updates = data.get("updates", [])
    updates.append({"message": msg, "consumed": False, "source": "telegram_reply"})
    data["updates"] = updates
    updates_file.write_text(yaml.dump(data, allow_unicode=True))
