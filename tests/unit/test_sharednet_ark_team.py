"""The Room loop bound to a real (mocked-subprocess) Orchestrator.

Uses the same integration fixture as ``test_paper_pipeline_mocked`` — the
OpenHands subprocess, pdflatex, git are mocked at the subprocess level — so
``run_agent`` is ARK's real one, and the Room is the in-process stand-in of
the SharedNet V1 API.
"""

import json
from unittest.mock import patch

import pytest
import yaml

from ark.sharednet.ark_team import run_room_team, sharednet_settings
from ark.sharednet.typed import DONE, WORK_REQUEST, WORK_RESULT, decode
from tests.conftest import MockController
from tests.fake_sharednet import FakeSharedNet

pytestmark = pytest.mark.integration


class HandoffController(MockController):
    """ARK's mock agents, now ending their message with a HANDOFF line.

    The reviewer scores 6.5 the first time and 8.5 the second, so the route is
    writer → reviewer → planner → writer → reviewer → done, decided hop by hop.
    """

    def _agent_stdout(self, agent_type: str, prompt: str) -> str:
        if agent_type == "reviewer":
            self.review_score = 6.5 if self._reviewer_call_count == 0 else 8.5
        base = super()._agent_stdout(agent_type, prompt)
        if agent_type == "reviewer":
            done = self.review_score >= 8.0
            handoff = {"next": None if done else "planner", "done": done,
                       "reason": "above threshold" if done else "two Major issues need a plan"}
        elif agent_type == "planner":
            handoff = {"next": "writer", "done": False, "reason": "all issues are WRITING_ONLY"}
        elif agent_type == "writer":
            handoff = {"next": "reviewer", "done": False, "reason": "revised; please re-review"}
        else:
            handoff = {"next": "writer", "done": False, "reason": ""}
        return f"{base}\nHANDOFF: {json.dumps(handoff)}"

    def _write_review(self):
        # keep the review file's score in step with what the agent said
        super()._write_review()


@pytest.fixture
def fake():
    with FakeSharedNet() as server:
        yield server


@pytest.fixture(autouse=True)
def _mock_telegram():
    with patch("ark.telegram.TelegramConfig.is_configured", new_callable=lambda: property(lambda self: False)):
        yield


def _typed(fake):
    rows = []
    for message in fake.store.transcript():
        text, envelope = decode(message["content"])
        rows.append((message["sender"]["name"], envelope.type if envelope else None,
                     envelope.fields if envelope else None))
    return rows


def test_settings_come_from_config_or_env(monkeypatch):
    assert sharednet_settings({}) is None
    assert sharednet_settings({"sharednet": {"invite": "x"}}) == {"invite": "x"}
    monkeypatch.setenv("SHAREDNET_INVITE", "ROOM=rom_a TOKEN=rit_b")
    assert sharednet_settings({"sharednet": {"invite": "x", "max_hops": 3}}) == {
        "invite": "ROOM=rom_a TOKEN=rit_b", "max_hops": 3}
    assert sharednet_settings({})["invite"] == "ROOM=rom_a TOKEN=rit_b"


def test_ark_agents_run_as_room_members(mock_integration_project_factory, fake):
    orch, controller = mock_integration_project_factory(controller_cls=HandoffController)
    orch.config["sharednet"] = {
        "invite": f"ROOM={fake.room_id} TOKEN={fake.invite} BASE={fake.base_url}",
        "max_hops": 8,
    }

    result = run_room_team(orch)

    assert result.done is True
    assert result.route == ["writer", "reviewer", "planner", "writer", "reviewer"]
    assert controller.agent_calls == ["writer", "reviewer", "planner", "writer", "reviewer"]

    rows = _typed(fake)
    assert [kind for _, kind, _ in rows] == [WORK_REQUEST, WORK_RESULT] * 5 + [DONE]
    names = sorted({name for name, _, _ in rows})
    assert names == ["ark:test_integ", "planner", "reviewer", "writer"]
    results = [fields for _, kind, fields in rows if kind == WORK_RESULT]
    assert results[1]["score"] == 6.5 and results[1]["next"] == "planner" and results[1]["decided_by"] == "agent"
    assert results[4]["score"] == 8.5 and results[4]["done"] is True

    # ARK's own state moved with it
    paper_state = orch.load_paper_state()
    assert [r["score"] for r in paper_state["reviews"]] == [6.5, 8.5]
    assert paper_state["current_score"] == 8.5
    assert paper_state["status"] == "accepted"
    assert orch.memory.scores == [6.5, 8.5]
    summary = yaml.safe_load((orch.state_dir / "sharednet_room.yaml").read_text())
    assert summary["room_id"] == fake.room_id and summary["done"] is True
    assert [h["role"] for h in summary["hops"]] == result.route

    # the planner saw the review, the writer saw the plan
    planner_task = fake.store.transcript()[4]["content"]
    assert "latest_review.md" in planner_task and "6.5/10" in planner_task
    writer_task = fake.store.transcript()[6]["content"]
    assert "M1 [WRITING_ONLY] Need more experiments" in writer_task


def test_reviewer_done_below_threshold_is_overridden(mock_integration_project_factory, fake):
    class EagerReviewer(HandoffController):
        def _agent_stdout(self, agent_type, prompt):
            out = super()._agent_stdout(agent_type, prompt)
            if agent_type == "reviewer" and self._reviewer_call_count == 1:
                # says done at 6.5 — the threshold guard must refuse
                out = out.replace('"done": false', '"done": true').replace('"next": "planner"', '"next": null')
            return out

    orch, controller = mock_integration_project_factory(controller_cls=EagerReviewer)
    orch.config["sharednet"] = {"invite": f"ROOM={fake.room_id} TOKEN={fake.invite} BASE={fake.base_url}",
                                "max_hops": 8}
    result = run_room_team(orch)
    assert result.done is True
    results = [fields for _, kind, fields in _typed(fake) if kind == WORK_RESULT]
    assert results[1]["done"] is False and results[1]["decided_by"] == "policy"
    assert "guard disagrees" in results[1]["reason"]
    assert result.route == ["writer", "reviewer", "planner", "writer", "reviewer"]


def test_orchestrator_run_takes_the_room_branch(mock_integration_project_factory, fake):
    orch, controller = mock_integration_project_factory(controller_cls=HandoffController)
    orch.config["sharednet"] = {"invite": f"ROOM={fake.room_id} TOKEN={fake.invite} BASE={fake.base_url}",
                                "max_hops": 8}
    with patch.object(orch, "check_dependencies", return_value=None), \
         patch.object(orch, "_run_ethical_review", return_value=True), \
         patch.object(orch, "start_telegram_listener", return_value=None), \
         patch.object(orch, "stop_telegram_listener", return_value=None), \
         patch.object(orch, "_send_session_banner", return_value=None), \
         patch.object(orch, "_ensure_project_env", return_value=None), \
         patch.object(orch, "_should_run_research_phase", return_value=False), \
         patch.object(orch, "_rehydrate_state_docs", return_value=None), \
         patch.object(orch, "_rehydrate_result_artifacts", return_value=None), \
         patch.object(orch, "run_paper_iteration") as legacy_loop, \
         patch.object(orch, "_run_dev_phase") as dev_phase:
        orch.run()
    assert legacy_loop.call_count == 0 and dev_phase.call_count == 0
    assert controller.agent_calls == ["writer", "reviewer", "planner", "writer", "reviewer"]
    assert _typed(fake)[-1][1] == DONE
