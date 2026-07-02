#!/usr/bin/env python3
"""Fake ``openhands`` CLI — the ONLY mock in the container e2e.

The real ARK orchestrator shells out to the ``openhands`` binary for every heavy
agent (``ark/engines/cli.py`` :: ``OpenHandsCLI.build_command`` invokes the bare
string ``"openhands"``). By putting this script earlier on ``$PATH`` than the
real binary, the *entire* real engine path still runs — ``build_command`` /
``build_env`` / the ``subprocess.Popen`` stream loop / ``parse_output`` — and only
the LLM-backed agent loop is replaced with canned, deterministic output.

It is a standalone port of ``tests/conftest.py`` :: ``MockController`` so the
container run exercises the same synthetic agent behaviour the unit/integration
suite already trusts, but as a real separate process talking to a real control
plane over the ``/v1`` HTTP boundary.

Invoked by the engine as:
    openhands --headless --json --override-with-envs -t "<task text>"

Contract it must honour (see OpenHandsCLI.parse_output / _read_usage):
  * stdout: a ``Conversation ID: <id>`` line, then a ``MessageEvent``/agent JSONL
    line whose text is the agent's "result".
  * cost:  ``$HOME/.openhands/conversations/<id>/base_state.json`` with
    ``stats.usage_to_metrics`` (unless ``ARK_OPENHANDS_CONV_DIR`` overrides root).
  * side effects: reviewer writes ``latest_review.md``; planner writes
    ``action_plan.yaml`` — both under the run's state dir, relative to cwd
    (the engine runs us with ``cwd=code_dir``).
"""

import json
import os
import sys
from pathlib import Path

# A cheap per-invocation id without Math.random/Date (unavailable) — the pid is
# unique enough across the sequential agent calls in one run.
CONV_ID = f"e2econv{os.getpid():08d}"

# State dir the orchestrator reads review/plan from: <code_dir>/auto_research/state.
# The engine runs us with cwd=code_dir; allow an explicit override for safety.
STATE_DIR = Path(os.environ.get("ARK_E2E_STATE_DIR", "auto_research/state"))

FILLER = ("\nThe agent has completed the requested task successfully. All files "
          "have been updated according to the instructions provided. No errors "
          "were encountered during execution.\n")

AGENT_TYPES = ["visualizer", "reviewer", "planner", "writer", "experimenter",
               "researcher", "meta_debugger", "coder"]

REVIEW_SCORE = float(os.environ.get("ARK_E2E_REVIEW_SCORE", "7.0"))
COST_PER_CALL = float(os.environ.get("ARK_E2E_COST_PER_CALL", "0.025"))


def _task_from_argv(argv) -> str:
    """Extract the task text passed after ``-t`` (the whole prompt lives there)."""
    for i, a in enumerate(argv):
        if a == "-t" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--task="):
            return a.split("=", 1)[1]
    # Fall back to the joined argv so keyword detection still works.
    return " ".join(argv)


def detect_agent(task: str) -> str:
    """Mirror MockController._detect_agent: prefer the explicit [AGENT:x] marker
    the scaffold's prompt files carry, then fall back to keyword heuristics."""
    for at in AGENT_TYPES:
        if f"[AGENT:{at}]" in task or f"{at}.prompt" in task:
            return at
    low = task.lower()
    if "review" in low:
        return "reviewer"
    if "planner" in low or "action_plan" in low:
        return "planner"
    if "writer" in low or "writing" in low:
        return "writer"
    if "figure_config.json" in low or "visualizer" in low:
        return "visualizer"
    return "unknown"


def agent_text(agent_type: str) -> str:
    """Mirror MockController._agent_stdout: agent-appropriate 'result' text the
    orchestrator parses (score line for reviewer, sentinels for others)."""
    if agent_type == "reviewer":
        return (f"Overall Score: {REVIEW_SCORE}/10\n"
                f"Review report saved to auto_research/state/latest_review.md\n{FILLER}")
    if agent_type == "planner":
        return f"Generated action_plan.yaml containing all issues to be addressed\n{FILLER}"
    if agent_type == "writer":
        return f"Updated main.tex modified Introduction and Results sections\n{FILLER}"
    if agent_type == "visualizer":
        return (f"FIGURES_OK all figure quality checks passed correct dimensions "
                f"clear fonts\n{FILLER}")
    if agent_type == "meta_debugger":
        return f"CONTINUE system status normal no issues requiring repair found\n{FILLER}"
    return f"done task completed successfully\n{FILLER}"


def write_review() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "latest_review.md").write_text(
        f"# Review Report\n\nOverall Score: {REVIEW_SCORE}/10\n\n"
        f"## Major Issues\n### M1. Need more experiments\n### M2. Improve writing\n\n"
        f"## Minor Issues\n### m1. Fix typos\n")


def write_action_plan() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    plan = {"issues": [
        {"id": "M1", "type": "WRITING_ONLY", "title": "Need more experiments",
         "status": "pending", "actions": [{"agent": "writer", "task": "update"}]},
        {"id": "M2", "type": "WRITING_ONLY", "title": "Improve writing",
         "status": "pending", "actions": [{"agent": "writer", "task": "polish"}]},
    ]}
    try:
        import yaml
        (STATE_DIR / "action_plan.yaml").write_text(
            yaml.dump(plan, default_flow_style=False, allow_unicode=True))
    except Exception:
        # Minimal hand-rolled YAML fallback if pyyaml is unavailable.
        lines = ["issues:"]
        for it in plan["issues"]:
            lines += [f"- id: {it['id']}", f"  type: {it['type']}",
                      f"  title: {it['title']}", f"  status: {it['status']}",
                      "  actions:", f"  - agent: {it['actions'][0]['agent']}",
                      f"    task: {it['actions'][0]['task']}"]
        (STATE_DIR / "action_plan.yaml").write_text("\n".join(lines) + "\n")


def write_cost_state() -> None:
    """Persist the token/cost state the engine's _read_usage reads back."""
    root = os.environ.get("ARK_OPENHANDS_CONV_DIR")
    base = Path(root) if root else Path.home() / ".openhands" / "conversations"
    d = base / CONV_ID
    d.mkdir(parents=True, exist_ok=True)
    (d / "base_state.json").write_text(json.dumps({"stats": {"usage_to_metrics": {
        "agent": {
            "accumulated_cost": COST_PER_CALL,
            "accumulated_token_usage": {
                "model": "e2e-fake-model", "prompt_tokens": 100,
                "completion_tokens": 50, "cache_read_tokens": 800,
                "cache_write_tokens": 200,
            },
        },
    }}}))


def main() -> int:
    task = _task_from_argv(sys.argv[1:])
    agent_type = detect_agent(task)

    if agent_type == "reviewer":
        write_review()
    elif agent_type == "planner":
        write_action_plan()
    write_cost_state()

    # Emit the OpenHands-format stream the engine parses: a Conversation ID line
    # then a MessageEvent/agent JSONL line carrying the result text.
    sys.stdout.write(f"Conversation ID: {CONV_ID}\n")
    sys.stdout.write(json.dumps({
        "kind": "MessageEvent", "source": "agent",
        "llm_message": {"content": [{"type": "text", "text": agent_text(agent_type)}]},
    }) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
