"""Hosted workflow boot and transitions with the real pinned DSH runtime."""

from datetime import datetime, timedelta
import os
from types import SimpleNamespace

import pytest

from ark.workflow import ResearchDecision
from ark.orchestrator.dsh_workflow import hosted_workflow_enabled, run_workflow


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
    monkeypatch.setenv("ARK_WORKFLOW_RUNTIME", "legacy")
    assert not uses_native_project(project)


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
        run_native_project(project, message="Explain the previous result")
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
