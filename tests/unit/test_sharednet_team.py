"""The Room team router: typed hand-offs, agent decisions, guard-rails, resume."""

import json

import pytest

from ark.sharednet.room import RoomClient
from ark.sharednet.team import DEFAULT_SUCCESSOR, HopContext, RoomTeam
from ark.sharednet.typed import DONE, STOPPED, WORK_REQUEST, WORK_RESULT, decode
from tests.fake_sharednet import FakeSharedNet

pytestmark = pytest.mark.unit

ROLES = ("experimenter", "writer", "reviewer", "planner")


@pytest.fixture
def fake():
    with FakeSharedNet() as server:
        yield server


def scripted(script: dict[str, list[str]]):
    """run_agent that pops the next scripted output per role and records calls."""
    calls: list[tuple[str, str]] = []

    def run_agent(role: str, task: str) -> str:
        calls.append((role, task))
        outputs = script.get(role) or []
        return outputs.pop(0) if outputs else f"{role} done\nHANDOFF: {{\"next\": \"{DEFAULT_SUCCESSOR[role]}\", \"done\": false}}"

    run_agent.calls = calls  # type: ignore[attr-defined]
    return run_agent


def handoff(next_role, done=False, reason=""):
    return "HANDOFF: " + json.dumps({"next": next_role, "done": done, "reason": reason})


def typed_transcript(fake):
    out = []
    for message in fake.store.transcript():
        text, envelope = decode(message["content"])
        out.append((message["sender"]["name"], envelope.type if envelope else None,
                    dict(envelope.fields) if envelope else None, message))
    return out


def make_team(fake, run_agent, **kwargs):
    kwargs.setdefault("log", lambda line: None)
    return RoomTeam(fake.base_url, fake.room_id, fake.invite, run_agent, roles=ROLES, **kwargs)


def test_agents_route_the_work_and_declare_done(fake):
    run_agent = scripted({
        "experimenter": [f"ran the sweep, results in findings.yaml\n{handoff('writer', reason='numbers are in')}"],
        "writer": [f"drafted sections 4-5\n{handoff('reviewer', reason='ready for review')}"],
        "reviewer": [f"Overall Score: 8.5/10\nSolid.\n{handoff(None, done=True, reason='above the bar')}"],
    })
    team = make_team(fake, run_agent)
    result = team.run("Write the TierKV paper", start_role="experimenter")

    assert result.done is True
    assert result.reason == "above the bar"
    assert result.route == ["experimenter", "writer", "reviewer"]
    assert [call[0] for call in run_agent.calls] == ["experimenter", "writer", "reviewer"]
    assert "HANDOFF:" in run_agent.calls[0][1], "the hand-off instruction is appended to every task"

    transcript = typed_transcript(fake)
    kinds = [(sender, kind) for sender, kind, _, _ in transcript]
    assert kinds == [
        ("ark-orchestrator", WORK_REQUEST), ("experimenter", WORK_RESULT),
        ("ark-orchestrator", WORK_REQUEST), ("writer", WORK_RESULT),
        ("ark-orchestrator", WORK_REQUEST), ("reviewer", WORK_RESULT),
        ("ark-orchestrator", DONE),
    ]
    # every result replies to its request; every decision is the agent's
    for index in (1, 3, 5):
        assert transcript[index][3]["reply_to_message_id"] == transcript[index - 1][3]["id"]
        assert transcript[index][2]["decided_by"] == "agent"
    assert transcript[1][2]["next"] == "writer"
    assert transcript[5][2] == {"next": None, "done": True, "reason": "above the bar",
                                "decided_by": "agent", "hop": 3}
    assert transcript[6][2] == {"reason": "above the bar", "hops": 3}
    # requests are addressed
    assert transcript[0][2] == {"to": "experimenter", "hop": 1}
    assert transcript[0][3]["content"].startswith("@experimenter hop 1")


def test_policy_takes_over_when_the_decision_is_missing_or_invalid(fake):
    run_agent = scripted({
        "experimenter": ["no hand-off line at all"],
        "writer": [f"asks a stranger\n{handoff('designer')}"],
        "reviewer": [f"Overall Score: 9/10\n{handoff(None, done=True, reason='ship it')}"],
    })
    team = make_team(fake, run_agent)
    result = team.run("goal", start_role="experimenter")
    assert result.route == ["experimenter", "writer", "reviewer"]
    decisions = [d for _, kind, d, _ in typed_transcript(fake) if kind == WORK_RESULT]
    assert decisions[0]["decided_by"] == "policy" and decisions[0]["next"] == "writer"
    assert decisions[1]["decided_by"] == "policy" and decisions[1]["next"] == "reviewer"
    assert "designer" in decisions[1]["reason"]
    assert decisions[2]["decided_by"] == "agent" and decisions[2]["done"] is True


def test_done_guard_overrides_a_premature_done(fake):
    run_agent = scripted({
        "reviewer": [
            f"Overall Score: 6/10\n{handoff(None, done=True, reason='good enough')}",
            f"Overall Score: 8.5/10\n{handoff(None, done=True, reason='now it is')}",
        ],
        "planner": [f"plan written\n{handoff('writer')}"],
        "writer": [f"revised\n{handoff('reviewer')}"],
    })

    def guard(role, output):
        import re
        score = float(re.search(r"Score: ([\d.]+)/10", output).group(1))
        return score >= 8.0

    team = make_team(fake, run_agent, done_guard=guard)
    result = team.run("goal", start_role="reviewer")
    assert result.done is True
    assert result.route == ["reviewer", "planner", "writer", "reviewer"]
    results = [d for _, kind, d, _ in typed_transcript(fake) if kind == WORK_RESULT]
    assert results[0]["done"] is False and results[0]["decided_by"] == "policy"
    assert "guard disagrees" in results[0]["reason"]
    assert results[0]["score"] == 6.0 and results[-1]["score"] == 8.5
    assert results[-1]["done"] is True and results[-1]["decided_by"] == "agent"


def test_hop_cap_and_self_handoff_cap(fake):
    run_agent = scripted({"writer": [f"more\n{handoff('writer')}" for _ in range(10)]})
    team = make_team(fake, run_agent, max_hops=4, max_consecutive_same_role=2)
    result = team.run("goal", start_role="writer")
    assert result.done is False
    assert "hop cap 4" in result.reason
    assert result.route == ["writer", "writer", "reviewer", "planner"]
    last = typed_transcript(fake)[-1]
    assert last[1] == STOPPED and last[2]["hops"] == 4 and last[2]["next"] == "writer"
    results = [d for _, kind, d, _ in typed_transcript(fake) if kind == WORK_RESULT]
    assert results[0]["decided_by"] == "agent" and results[0]["next"] == "writer"
    assert results[1]["decided_by"] == "policy" and results[1]["next"] == "reviewer"


def test_room_guidance_reaches_the_next_agent(fake):
    run_agent = scripted({
        "writer": [f"drafted\n{handoff('reviewer')}"],
        "reviewer": [f"Overall Score: 9/10\n{handoff(None, done=True)}"],
    })
    team = make_team(fake, run_agent)
    team.join_all()
    # A human in the Web UI speaks before the run starts, and again mid-run.
    fake.store.speak_as_human("xisen", "Keep the paper to 8 pages.")

    original = run_agent

    def run_and_interject(role, task):
        if role == "writer":
            fake.store.speak_as_human("xisen", "Reviewer: be strict about figure 2.")
        return original(role, task)

    team.run_agent = run_and_interject
    team.run("goal", start_role="writer")
    writer_task = original.calls[0][1]
    reviewer_task = original.calls[1][1]
    assert "Room guidance" in writer_task and "8 pages" in writer_task
    assert "figure 2" in reviewer_task and "8 pages" not in reviewer_task, "guidance is delivered once"


def test_resume_from_the_room_log(fake):
    first = scripted({
        "writer": [f"drafted\n{handoff('reviewer')}"],
        "reviewer": [f"Overall Score: 7/10\n{handoff('planner', reason='two majors')}"],
    })
    team = make_team(fake, first, max_hops=2)
    stopped = team.run("goal", start_role="writer")
    assert stopped.done is False and stopped.route == ["writer", "reviewer"]

    # A new process, same Room, no local state: continues with the planner.
    second = scripted({
        "planner": [f"planned\n{handoff('writer')}"],
        "writer": [f"revised\n{handoff(None, done=True, reason='final')}"],
    })
    team2 = make_team(fake, second, max_hops=10)
    resumed = team2.run("goal", start_role="experimenter")
    assert resumed.done is True
    assert resumed.route == ["planner", "writer"]
    assert [h.hop for h in resumed.hops] == [3, 4], "hop numbers continue from the log"

    # And a third process finds the log finished and calls no Agent.
    third = scripted({})
    assert make_team(fake, third).run("goal").done is True
    assert third.calls == []


def test_task_builder_sees_the_previous_hop(fake):
    seen: list[HopContext] = []

    def builder(context: HopContext) -> str:
        seen.append(context)
        return f"[{context.role}] {context.goal}"

    run_agent = scripted({
        "writer": [f"draft\n{handoff('reviewer', reason='please check')}"],
        "reviewer": [f"Overall Score: 9/10\n{handoff(None, done=True)}"],
    })
    make_team(fake, run_agent, task_builder=builder).run("G", start_role="writer")
    assert [c.role for c in seen] == ["writer", "reviewer"]
    assert seen[1].previous_role == "writer"
    assert seen[1].previous_decision.reason == "please check"
    assert "draft" in seen[1].previous_output
    assert run_agent.calls[1][1].startswith("[reviewer] G")


def test_members_are_distinct_room_members(fake):
    team = make_team(fake, scripted({}))
    team.join_all()
    names = sorted(m["name"] for m in fake.store.members.values())
    assert names == sorted(["ark-orchestrator", *ROLES])
    assert len(team.member_ids) == len(ROLES) + 1
    assert all(isinstance(c, RoomClient) and c.member_id for c in team.members.values())
