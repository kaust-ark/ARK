"""Hosted workflow boot and transitions with the real pinned DSH runtime."""

from datetime import datetime, timedelta
import json
import os
from types import SimpleNamespace

import pytest

from ark.workflow import ResearchDecision
from ark.orchestrator.dsh_workflow import hosted_workflow_enabled, run_workflow


def test_pack_cache_keys_the_staged_wheel_and_npm_inputs(tmp_path, monkeypatch):
    from pathlib import Path
    from subprocess import CompletedProcess
    from integrations.dsh import pack

    repo = tmp_path / "repo"
    plugin = repo / "integrations/dsh"
    monkeypatch.setattr(pack, "HERE", plugin)
    monkeypatch.setattr(pack, "REPO", repo)
    def write(path, text):
        file = repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
        return file

    for name in ("LICENSE", "pyproject.toml", "ark/orchestrator/dsh_workflow.mjs",
                 "website/dashboard/templates/research.css", "skills/paper/SKILL.md", "venue_templates/icml/main.tex",
                 "integrations/dsh/LICENSE", "integrations/dsh/README.md", "integrations/dsh/index.js"):
        write(name, "initial")
    write("integrations/dsh/package.json", json.dumps({"files": ["index.js", "companion"]}))
    builds = []
    fail_pack = False
    def run(cmd, **kwargs):
        if "wheel" in cmd:
            out = Path(cmd[cmd.index("--wheel-dir") + 1])
            (out / "ark.whl").write_bytes(b"wheel")
        elif cmd[1] == "pack":
            target = Path(cmd[-1]) / "idea2paper-dsh-1.tgz"
            target.write_bytes(b"tar")
            if fail_pack:
                raise RuntimeError("interrupted pack")
            builds.append(target.name)
            return CompletedProcess(cmd, 0, json.dumps([{"filename": target.name}]))
        return CompletedProcess(cmd, 0)
    monkeypatch.setattr(pack.subprocess, "run", run)

    cache = tmp_path / "cache"
    first = pack.build(cache, cached=True)
    write("website/__pycache__/app.pyc", "unrelated")
    write("skills/.pytest_cache/v/cache/nodeids", "unrelated")
    write("integrations/dsh/project.test.js", "outside the npm files list")
    assert pack.build(cache, cached=True) == first and len(builds) == 1
    previous = first
    for name in ("website/dashboard/templates/research.css", "ark/orchestrator/dsh_workflow.mjs",
                 "LICENSE", "integrations/dsh/README.md"):
        write(name, "changed")
        current = pack.build(cache, cached=True)
        assert current.parent != previous.parent and current.is_file()
        previous = current
    write("integrations/dsh/package.json", json.dumps({"files": ["index.js", "project.test.js", "companion"]}))
    fail_pack = True
    with pytest.raises(RuntimeError, match="interrupted pack"):
        pack.build(cache, cached=True)
    fail_pack = False
    assert pack.build(cache, cached=True).parent != previous.parent and len(builds) == 6


@pytest.mark.parametrize(("override", "budget"), [(None, 1), ("0", 0), ("3", 3)])
def test_native_profile_retries_only_transient_provider_failures(tmp_path, monkeypatch, override, budget):
    from ark.orchestrator.dsh_workflow import _native_profile
    from ark.engines.dsh_runtime import DSHRuntime

    monkeypatch.setattr(DSHRuntime, "build_env", lambda *args: {})
    monkeypatch.delenv("ARK_DSH_API", raising=False)
    monkeypatch.delenv("ARK_DSH_RETRIES", raising=False)
    monkeypatch.setenv("ARK_DSH_CONTEXT_WINDOW", "65536")
    if override is not None:
        monkeypatch.setenv("ARK_DSH_RETRIES", override)
    _native_profile(SimpleNamespace(config={}, model="deepseek/deepseek-v4-pro"), tmp_path, tmp_path)
    rows = {r["id"]: r for r in json.loads((tmp_path / "cordis.patch.yml").read_text()) if "id" in r}
    policy = rows["llm-deepseek"]["config"]["retryPolicy"]
    assert rows["llm-retry"]["disabled"] == (budget == 0)
    assert policy["maxRetries"] == budget and policy["mode"] == "normal"
    assert set(policy["retryableCodes"]) == {"SERVER", "TIMEOUT", "TRANSPORT"}


def test_hosted_uses_dsh_with_rollback(monkeypatch):
    monkeypatch.delenv("ARK_AGENT_RUNTIME", raising=False)
    monkeypatch.delenv("ARK_WORKFLOW_RUNTIME", raising=False)
    assert hosted_workflow_enabled("project-id")
    assert not hosted_workflow_enabled(None)
    monkeypatch.setenv("ARK_WORKFLOW_RUNTIME", "python")
    assert not hosted_workflow_enabled("project-id")
    monkeypatch.setenv("ARK_WORKFLOW_RUNTIME", "dsh")
    monkeypatch.setenv("ARK_AGENT_RUNTIME", "cli")
    assert not hosted_workflow_enabled("project-id")


@pytest.mark.parametrize("fail_execute", [False, True])
def test_real_hosted_workflow_resumes_python_checkpoint(monkeypatch, fail_execute):
    interpreter = os.environ.get("ARK_DSH_TEST_PYTHON")
    if not interpreter:
        pytest.skip("Set ARK_DSH_TEST_PYTHON to exercise the pinned runtime")
    monkeypatch.setenv("ARK_DSH_PYTHON", interpreter)
    calls = []

    def record(label, result=None):
        def run(*args, **kwargs):
            calls.append((label, args, kwargs))
            return result
        return run

    state = {"iteration": 1, "status": "in_progress", "steps_done": ["plan"]}
    project = SimpleNamespace(
        iteration=1, max_iterations=2, max_end_time=datetime.now() + timedelta(minutes=2),
        _stop_requested=False, _asked_this_iteration=False, _terminal_error=None,
        _quota_exhausted=fail_execute, paper_accept_threshold=8, hooks=None,
        _research_idea="Find a small measurable question", log=lambda *_: None,
        log_section=lambda *_: None, log_step=lambda *_: None,
        _prepare_research_workspace=record("setup", True),
        _should_run_research_phase=lambda: False, _run_research_phase=record("research"),
        _get_max_iteration_target=lambda: 2, _should_run_dev_phase=lambda: True,
        _begin_dev_phase=lambda: (state, 0, 1), _load_findings_summary=lambda: "findings",
        _save_dev_phase_state=lambda *_: None, _send_dev_phase_telegram=lambda *_: None,
        _abort_dev_on_terminal_error=lambda *_: False, _plan_experiments=record("dev-plan"),
        _experiment_approval_gate=record("approve"), _run_experiments=record("dev-run"),
        _analyze_results=record("dev-analyze"), _evaluate_completeness=record("dev-evaluate", ResearchDecision("proceed_to_writing", [])),
        _start_initial_draft=record("writing"), _generate_all_figures=record("figures"),
        _write_initial_draft=record("draft"), _deliver_dev_phase=record("deliver"),
        load_paper_state=lambda: {"current_score": 6}, get_resume_step=lambda: 3,
        _step_compile=record("compile", True), _step_review=record("review", ("existing", 7, [])),
        _check_acceptance=lambda *_: (False, False, False), _step_plan=record("plan", ({}, "")),
        _step_execute=record("execute"), _handle_quota_exhausted=record("quota", False),
        _step_validate=record("validate"), _iteration_finalize=record("finalize", False),
        _handle_terminal_error=record("terminal", False),
    )
    project._iteration_prep = lambda: setattr(project, "iteration", project.iteration + 1)
    assert run_workflow(project) == 1
    labels = [label for label, *_ in calls]
    assert labels == ["setup", "dev-run", "dev-analyze", "dev-evaluate", "writing", "figures", "draft", "deliver",
                      "compile", "review", "plan", "execute"] + (
        ["quota"] if fail_execute else ["validate", "finalize"]
    )
    assert calls[1][2]["resumed"] is True
    assert next(args for label, args, _ in calls if label == "execute") == (4, 5, 3, {}, "")
    assert state["steps_done"] == ["plan", "run", "analyze", "evaluate"]


def test_hosted_uses_native_for_fresh_project_and_retains_legacy(tmp_path, monkeypatch):
    from ark.orchestrator.dsh_workflow import uses_native_project

    state = tmp_path / "auto_research/state"
    state.mkdir(parents=True)
    project = SimpleNamespace(code_dir=tmp_path, state_dir=state)
    assert uses_native_project(project)
    (state / "dev_phase_state.yaml").write_text("steps_done: [plan]\n")
    assert not uses_native_project(project)
    (tmp_path / "workflow.json").write_text('{"version":3}')
    assert uses_native_project(project)
    for policy in ("python", "legacy"):
        monkeypatch.setenv("ARK_WORKFLOW_RUNTIME", policy)
        monkeypatch.setenv("ARK_AGENT_RUNTIME", "cli")
        assert hosted_workflow_enabled("project-id", project)
        assert uses_native_project(project)
    (tmp_path / "workflow.json").unlink()
    assert not hosted_workflow_enabled("project-id", project)
    assert not uses_native_project(project)


def test_hosted_retains_early_legacy_worker_after_log_rotation(tmp_path, monkeypatch):
    from ark.orchestrator.dsh_workflow import uses_native_project

    state = tmp_path / "auto_research/state"
    state.mkdir(parents=True)
    logs = state.parent / "logs"
    logs.mkdir()
    own = logs / "hosted-id_paper_20261119_183132.log"
    project = SimpleNamespace(code_dir=tmp_path, state_dir=state, _project_id="hosted-id", log_file=own)
    (state / "idea.md").write_text("Copied project idea")
    (state / "user_prefs.yaml").write_text("mode: paper")
    gate = state / "ethical_review.json"
    gate.write_text('{"verdict":"proceed"}')
    (logs / "other-id_paper_20261119_183132.log").write_text("another project")
    (logs / "hosted-id_paper_notes.log").write_text("copied notes")
    (logs / "hosted-id_paper_20261119_180000.log").mkdir()
    (logs / "hosted-id_paper_20261119_180001.log").symlink_to(own)
    os.mkfifo(logs / "hosted-id_paper_20261119_180002.log")
    own.write_text("Current worker started")
    assert uses_native_project(project)

    old = logs / "hosted-id_paper_20261119_183100.log"
    with old.open("wb") as stream:
        stream.truncate(2**32)
    old.chmod(0)
    assert not uses_native_project(project)
    old.unlink()
    gate.write_text('{"verdict":"proceed","reviewed":true}')
    assert not uses_native_project(project)

    pin = tmp_path / ".dsh/native/home/profiles/idea2paper/bundle.sha256"
    pin.parent.mkdir(parents=True)
    pin.write_text("a" * 64)
    monkeypatch.setenv("ARK_WORKFLOW_RUNTIME", "legacy")
    assert hosted_workflow_enabled("hosted-id", project)
    assert uses_native_project(project)
    pin.unlink()
    (tmp_path / "workflow.json").write_text('{"version":3}')
    assert uses_native_project(project)


def test_cli_legacy_identity_requires_its_own_run_log_pointer(tmp_path):
    from ark.orchestrator.dsh_workflow import uses_native_project

    state = tmp_path / "auto_research/state"
    state.mkdir(parents=True)
    logs = state.parent / "logs"
    logs.mkdir()
    project = SimpleNamespace(code_dir=tmp_path, state_dir=state, _project_id="db-uuid", project_name="cli-name")
    old = logs / "cli-name_paper_20261119_183100.log"
    old.write_text("DSH hosted workflow started")
    assert uses_native_project(project)

    latest = logs / "latest.log"
    latest.symlink_to("../elsewhere/" + old.name)
    assert uses_native_project(project)
    latest.unlink()
    latest.symlink_to(old.name)
    assert not uses_native_project(project)

    # The next Orchestrator replaces latest.log with its current empty log.
    latest.unlink()
    project.log_file = logs / "cli-name_paper_20261119_183200.log"
    project.log_file.touch()
    latest.symlink_to(project.log_file.name)
    assert not uses_native_project(project)
    old.unlink()
    assert uses_native_project(project)


def test_installed_bundle_retains_verified_project_pin_across_rollout(
    tmp_path, monkeypatch
):
    import hashlib
    import json
    import shutil
    import tarfile
    from ark.orchestrator.dsh_workflow import _install_bundle

    dist = tmp_path / "package"
    commander = dist / "node_modules/commander/package.json"
    commander.parent.mkdir(parents=True)
    commander.write_text("{}")
    (dist / "package.json").write_text(
        json.dumps({"name": "idea2paper-dsh", "engines": {"dsh": "0.2.1-rc.2"}})
    )
    profile = tmp_path / "profile"
    profile.mkdir()
    archives = []
    for version in ("first", "second"):
        (dist / "index.js").write_text(version)
        archive = tmp_path / f"{version}.tgz"
        with tarfile.open(archive, "w:gz") as output:
            output.add(dist, arcname="package")
        archives.append((archive, hashlib.sha256(archive.read_bytes()).hexdigest()))

    (first, old), (second, new) = archives
    monkeypatch.setenv("IDEA2PAPER_BUNDLE_PATH", str(first))
    monkeypatch.setenv("IDEA2PAPER_BUNDLE_SHA256", old)
    assert _install_bundle(profile) == old
    monkeypatch.setenv("IDEA2PAPER_BUNDLE_PATH", str(second))
    monkeypatch.setenv("IDEA2PAPER_BUNDLE_SHA256", new)
    assert _install_bundle(profile) == old
    package = profile / "node_modules/idea2paper-dsh"
    assert (package / "index.js").read_text() == "first"
    (package / "index.js").write_text("modified")
    with pytest.raises(RuntimeError, match=old):
        _install_bundle(profile)

    # Before the file manifest was introduced, the project kept only a digest.
    (profile / "bundle.files.json").unlink()
    with pytest.raises(RuntimeError, match=old):
        _install_bundle(profile)
    shutil.rmtree(package)
    with pytest.raises(RuntimeError, match=old):
        _install_bundle(profile)
    monkeypatch.setenv("IDEA2PAPER_BUNDLE_PATH", str(first))
    assert _install_bundle(profile) == old
    assert (package / "index.js").read_text() == "first"


@pytest.mark.parametrize("model", ["deepseek/deepseek-flash", "openai/fixture"])
def test_real_native_host_replays_owner_through_installed_tarball(
    tmp_path, monkeypatch, model
):
    """A real native owner uses the hosted pipe; all model requests stay on loopback."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from ark.orchestrator import native_domain
    from ark.orchestrator.dsh_workflow import run_native_project

    if not os.getenv("ARK_DSH_TEST_PYTHON") or not os.getenv("IDEA2PAPER_BUNDLE_PATH"):
        pytest.skip(
            "Set ARK_DSH_TEST_PYTHON and IDEA2PAPER_BUNDLE_PATH to test the packed hosted runtime"
        )
    requests, operations = [], []

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            if model.startswith("openai/"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                payload = {"id": "localhost", "object": "chat.completion.chunk",
                           "created": 1, "model": body["model"], "choices": [
                           {"index": 0, "delta": {"role": "assistant", "content": "Fixture project response"},
                            "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}
                self.wfile.write(("data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n").encode())
                return
            frames = [
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg-" + str(len(requests)),
                        "model": body["model"],
                        "usage": {"input_tokens": 5, "output_tokens": 0},
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Fixture project response"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 3},
                },
                {"type": "message_stop"},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                "".join(
                    f"event: {f['type']}\ndata: {json.dumps(f)}\n\n" for f in frames
                ).encode()
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Domain:
        def __init__(self, *_args, **_kwargs):
            pass

        def invoke(self, op, args):
            operations.append((op, args))
            if op == "project_status":
                return {"state": "running", "delivered": bool(requests), "commands": []}
            assert op == "project_event", op
            return {"recorded": True}

    monkeypatch.setattr(native_domain, "NativeProject", Domain)
    monkeypatch.setattr(
        "ark.orchestrator.dsh_workflow.native_delivery_current", lambda _: True
    )
    monkeypatch.setenv("ARK_DSH_PYTHON", os.environ["ARK_DSH_TEST_PYTHON"])
    monkeypatch.setenv("LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-fixture-only")
    monkeypatch.setenv("OPENAI_API_KEY", "local-fixture-only")
    monkeypatch.delenv("ARK_DSH_API", raising=False)
    project = SimpleNamespace(
        code_dir=tmp_path,
        state_dir=tmp_path / "auto_research/state",
        config={},
        model=model,
        _research_idea="A private hosted research idea",
        _terminal_error=None,
        _stop_requested=False,
        log=lambda *_: None,
    )
    project.state_dir.mkdir(parents=True)
    try:
        run_native_project(project)
        claim = json.loads((tmp_path / "workflow.json").read_text())
        from ark.pipeline import PipelineMixin
        monkeypatch.setenv("ARK_WORKFLOW_RUNTIME", "python")
        monkeypatch.setenv("ARK_AGENT_RUNTIME", "cli")
        assert hosted_workflow_enabled("project-id", project)
        assert PipelineMixin._native_followup(project, "Explain the previous result")
        assert (
            json.loads((tmp_path / "workflow.json").read_text())["owner_session_id"]
            == claim["owner_session_id"]
        )
        assert len(requests) == 2
        assert project._research_idea in str(requests[1]["messages"])
        assert "Explain the previous result" in str(requests[1]["messages"])
        assert any(
            op == "project_event" and args.get("kind") == "owner/response"
            for op, args in operations
        )
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def worker_bundle(tmp_path):
    import hashlib
    import tarfile
    import zipfile

    def make_bundle(version):
        package = tmp_path / version / "package"
        (package / "node_modules/commander").mkdir(parents=True)
        (package / "node_modules/commander/package.json").write_text("{}")
        (package / "package.json").write_text(json.dumps({"name":"idea2paper-dsh", "engines":{"dsh":"0.2.1-rc.2"}}))
        (package / "index.js").write_text(version)
        companion = package / "companion"
        companion.mkdir()
        wheel = companion / "fixture.whl"
        core = """
import json, os, sys
from pathlib import Path
def main():
    project = Path(sys.argv[sys.argv.index('--code-dir') + 1])
    package = project / '.dsh/native/home/profiles/idea2paper/node_modules/idea2paper-dsh'
    assert (package / 'index.js').read_text() == VERSION
    (project/'workflow.json').write_text('{"version":3}')
    (project/'worker-result.json').write_text(json.dumps([VERSION, __file__, os.environ['IDEA2PAPER_BUNDLE_SHA256']]))
"""
        with zipfile.ZipFile(wheel, "w") as ws:
            ws.writestr("ark/__init__.py", "")
            ws.writestr("ark/orchestrator/__init__.py", "")
            ws.writestr("ark/orchestrator/core.py", f"VERSION = {version!r}\n" + core)
        (companion / "manifest.json").write_text(json.dumps({
            "wheel":wheel.name, "sha256":hashlib.sha256(wheel.read_bytes()).hexdigest()}))
        archive = tmp_path / (version + ".tgz")
        with tarfile.open(archive, "w:gz") as out:
            out.add(package, arcname="package")
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    return make_bundle


def test_hosted_worker_keeps_python_and_js_pinned_after_service_rollout(tmp_path, worker_bundle):
    """Exercise the production module entry point across two different wheel payloads."""
    import subprocess
    import sys
    from pathlib import Path

    def launch(name, archive, deployed_digest):
        project = tmp_path / name
        project.mkdir(exist_ok=True)
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2]), "ARK_WORKFLOW_RUNTIME":"native",
               "ARK_AGENT_RUNTIME":"dsh", "IDEA2PAPER_BUNDLE_PATH":str(archive), "IDEA2PAPER_BUNDLE_SHA256":deployed_digest,
               "XDG_CACHE_HOME":str(tmp_path / "service-cache")}
        subprocess.run([sys.executable, "-m", "ark.orchestrator", "--project", name, "--project-id", name,
                        "--project-dir", str(project), "--code-dir", str(project), "--db-path", str(tmp_path / "db")],
                       env=env, cwd=tmp_path, check=True, capture_output=True, text=True, timeout=30)
        version, origin, digest = json.loads((project / "worker-result.json").read_text())
        assert "idea2paper-worker-" in origin
        return version, digest

    first, first_sha = worker_bundle("first")
    second, second_sha = worker_bundle("second")
    assert launch("original", first, first_sha) == ("first", first_sha)
    first.unlink()
    assert launch("original", second, second_sha) == ("first", first_sha)
    assert launch("fresh", second, second_sha) == ("second", second_sha)

    # Migrate an existing v3 profile whose service has only its older build cache.
    cache = tmp_path / "service-cache/idea2paper"
    original_archive = cache / "hosted-archives" / (first_sha + ".tgz")
    older_cache = cache / "bundles/source-input-key/original.tgz"
    older_cache.parent.mkdir(parents=True)
    original_archive.rename(older_cache)
    assert launch("original", second, second_sha) == ("first", first_sha)
    original_archive.unlink()
    older_cache.unlink()
    with pytest.raises(subprocess.CalledProcessError) as missing:
        launch("original", second, second_sha)
    assert first_sha in missing.value.stderr and "Restore the trusted Idea2Paper release" in missing.value.stderr


def test_cli_run_loads_the_original_worker_on_resume(tmp_path, monkeypatch, worker_bundle):
    import sys

    from ark import cli, paths

    monkeypatch.setattr(paths, "_ARK_ROOT", tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("ARK_WORKFLOW_RUNTIME", raising=False)
    monkeypatch.delenv("ARK_AGENT_RUNTIME", raising=False)
    config_dir = tmp_path / "projects" / "cli-pin"
    config_dir.mkdir(parents=True)
    workspace = tmp_path / "research"
    workspace.mkdir()
    (config_dir / "config.yaml").write_text(json.dumps({
        "code_dir": str(workspace), "_project_id": "test-cli-pin", "_db_path": str(tmp_path / "db")
    }))
    monkeypatch.setattr(sys, "argv", ["ark", "run", "cli-pin", "--no-research"])
    original_digest = None
    for version in ("initial", "rollout"):
        archive, digest = worker_bundle(version)
        monkeypatch.setenv("IDEA2PAPER_BUNDLE_PATH", str(archive))
        monkeypatch.setenv("IDEA2PAPER_BUNDLE_SHA256", digest)
        if version == "rollout":
            monkeypatch.setenv("ARK_WORKFLOW_RUNTIME", "legacy")
        cli.main()
        pid = int((config_dir / ".pid").read_text())
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        worker_version, origin, worker_digest = json.loads((workspace / "worker-result.json").read_text())
        assert worker_version == "initial" and "idea2paper-worker-" in origin
        if original_digest is None:
            original_digest = digest
        assert worker_digest == original_digest
        (workspace / "worker-result.json").unlink()
        archive.unlink()
