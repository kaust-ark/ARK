"""Exercise detached tmux servers with a local CLI stub; no LLM/API calls."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ark.chat_agent import load_conversation_id, run_chat_turn
from ark.engines.cli import OpenHandsCLI, _openhands_tmux_env


@pytest.fixture
def cli_stub(tmp_path):
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    script = tmp_path / "fake_openhands.py"
    script.write_text(textwrap.dedent('''\
        import json
        import os
        from pathlib import Path
        import subprocess
        import sys
        import time

        work = Path.cwd()
        (work / "terminal-root").write_text(os.environ["TMUX_TMPDIR"])
        (work / "argv.json").write_text(json.dumps(sys.argv[1:]))
        (work / "child-home").write_text(os.environ["HOME"])
        (work / "subdir").mkdir(exist_ok=True)

        def tmux(*args):
            subprocess.run(
                ["tmux", "-f", "/dev/null", "-L", "openhands", *args],
                check=True, stdout=subprocess.DEVNULL,
            )

        tmux("new-session", "-d", "-s", "agent", "/bin/sh")
        tmux("send-keys", "-t", "agent", "cd subdir; export ARK_PERSIST=kept", "Enter")
        tmux("send-keys", "-t", "agent",
             'printf "%s:%s" "$PWD" "$ARK_PERSIST" > ../shell-state', "Enter")
        deadline = time.monotonic() + 5
        while not (work / "shell-state").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("shell commands did not complete")
            time.sleep(0.01)
        (work / "ready").touch()
        mode = os.environ.get("ARK_TEST_MODE", "normal")
        if mode != "silent":
            print("Conversation ID: test-conversation", flush=True)
            print(json.dumps({"kind": "MessageEvent", "source": "agent",
                              "llm_message": {"content": [
                                  {"type": "text", "text": "done"}]}}), flush=True)
        if mode in ("hold", "silent"):
            while not (work / "release").exists():
                time.sleep(0.01)
        # Deliberately leak the detached tmux server, as a crashed CLI does.
        sys.exit(7 if mode == "failure" else 0)
    '''))
    return script


def _runner(script):
    class StubCLI(OpenHandsCLI):
        def build_command(self, prompt, path_boundary, code_dir):
            return [sys.executable, str(script)]

    return StubCLI("test-model")


def _wait_ready(work):
    deadline = time.monotonic() + 8
    while not (work / "ready").exists():
        if time.monotonic() > deadline:
            pytest.fail("stub failed to create a terminal")
        time.sleep(0.01)


def _server_exists(root):
    result = subprocess.run(
        ["tmux", "-S", str(root / f"tmux-{os.getuid()}" / "openhands"),
         "has-session", "-t", "agent"],
        capture_output=True, timeout=5,
    )
    return result.returncode == 0


@pytest.mark.parametrize("mode", ["normal", "failure", "silent", "abort", "interrupt", "terminate"])
def test_terminal_cleanup_on_every_exit(cli_stub, tmp_path, mode):
    env = dict(os.environ, ARK_TEST_MODE=("hold" if mode in ("abort", "interrupt", "terminate") else mode))
    original_env = dict(env)

    def on_event(line):
        if mode == "interrupt":
            raise KeyboardInterrupt
        if mode == "terminate":
            raise SystemExit(0)
        if mode == "abort":
            return "ABORT"

    def execute():
        return _runner(cli_stub).execute(
            "test", "", tmp_path, 2 if mode == "silent" else 10,
            env=env, on_event=on_event,
        )

    started = time.monotonic()
    if mode in ("interrupt", "terminate"):
        with pytest.raises(KeyboardInterrupt if mode == "interrupt" else SystemExit):
            execute()
    else:
        rc, stdout, stderr, _, timed_out = execute()
        assert timed_out == (mode == "silent")
        assert rc == (7 if mode == "failure" else -9 if mode in ("silent", "abort") else 0)
        assert "Traceback" not in stderr
        if mode == "normal":
            assert "done" in stdout
    assert time.monotonic() - started < 8
    assert env == original_env
    assert (tmp_path / "shell-state").read_text() == f"{tmp_path / 'subdir'}:kept"
    root = Path((tmp_path / "terminal-root").read_text())
    assert not _server_exists(root)
    assert not root.exists()


def test_parallel_invocations_do_not_clean_up_each_other(cli_stub, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    runner = _runner(cli_stub)
    env = dict(os.environ, ARK_TEST_MODE="hold")
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(runner.execute, "a", "", first, 15, env=env)
        b = pool.submit(runner.execute, "b", "", second, 15, env=env)
        try:
            _wait_ready(first)
            _wait_ready(second)
            root_a = Path((first / "terminal-root").read_text())
            root_b = Path((second / "terminal-root").read_text())
            assert root_a != root_b
            assert _server_exists(root_a) and _server_exists(root_b)
            (first / "release").touch()
            assert a.result(timeout=8)[0] == 0
            assert not _server_exists(root_a)
            assert _server_exists(root_b)
        finally:
            (first / "release").touch()
            (second / "release").touch()
        assert b.result(timeout=8)[0] == 0
    assert not _server_exists(root_b)


@pytest.mark.parametrize("conversation_id,mode", [(None, "normal"), ("existing", "normal"), ("existing", "silent")])
def test_chat_uses_managed_terminal(cli_stub, tmp_path, monkeypatch, conversation_id, mode):
    from ark.chat_agent import _ChatCLI

    build_command = _ChatCLI.build_command

    def stub_command(self, prompt, path_boundary, code_dir):
        return [sys.executable, str(cli_stub), *build_command(self, prompt, path_boundary, code_dir)[1:]]

    monkeypatch.setattr(_ChatCLI, "build_command", stub_command)
    monkeypatch.setenv("ARK_TEST_MODE", mode)
    answers, logs, steps = [], [], []
    answer, conv_id, ok = run_chat_turn(
        workspace=tmp_path, message="hello", model="test-model",
        conversation_id=conversation_id, on_answer=answers.append,
        on_step=steps.append, log=logs.append,
        timeout=2 if mode == "silent" else 10,
    )
    assert conv_id == (conversation_id or "test-conversation")
    assert load_conversation_id(tmp_path) == conv_id
    assert answer == ("" if mode == "silent" else "done")
    assert ok == (mode != "silent")
    assert answers == [answer]
    assert (tmp_path / "child-home").read_text() == str(tmp_path)
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert argv[-2:] == ["-t", "hello"]
    assert ("--resume" in argv) == bool(conversation_id)
    if conversation_id:
        assert argv[argv.index("--resume") + 1] == conversation_id
    if mode == "silent":
        assert "[chat] timed out" in logs
    else:
        assert steps
    root = Path((tmp_path / "terminal-root").read_text())
    assert not _server_exists(root)
    assert not root.exists()


def test_spawn_failure_cleans_private_directory(tmp_path, monkeypatch):
    roots = []

    def fail_spawn(*args, **kwargs):
        roots.append(Path(kwargs["env"]["TMUX_TMPDIR"]))
        raise FileNotFoundError("CLI missing")

    monkeypatch.setattr("ark.engines.cli.shutil.which", lambda *a, **kw: "/usr/bin/tmux")
    monkeypatch.setattr("ark.engines.cli.subprocess.Popen", fail_spawn)
    monkeypatch.setattr("ark.engines.cli.subprocess.run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "", ""))
    with pytest.raises(FileNotFoundError, match="CLI missing"):
        OpenHandsCLI("test-model").execute("test", "", tmp_path, 1)
    assert len(roots) == 1 and not roots[0].exists()


def test_no_tmux_keeps_subprocess_fallback(monkeypatch):
    monkeypatch.setattr("ark.engines.cli.shutil.which", lambda *a, **kw: None)
    env = {"PATH": "/nothing", "HOME": "/project"}
    with _openhands_tmux_env(env) as child:
        assert child == env
        assert child is not env


def test_cleanup_failure_preserves_socket_and_original_error(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("ark.engines.cli.shutil.which", lambda *a, **kw: "/usr/bin/tmux")
    monkeypatch.setattr("ark.engines.cli.subprocess.run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "permission denied"))
    root = None
    try:
        with pytest.raises(ValueError, match="original"):
            with _openhands_tmux_env(dict(os.environ)) as child:
                root = Path(child["TMUX_TMPDIR"])
                socket = root / f"tmux-{os.getuid()}" / "openhands"
                socket.parent.mkdir()
                socket.touch()
                raise ValueError("original")
        assert socket.exists()
        assert "Could not clean up OpenHands tmux socket" in caplog.text
    finally:
        if root:
            shutil.rmtree(root)
