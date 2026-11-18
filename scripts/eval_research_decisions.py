"""Compare pre-draft decisions with equal artifacts, Google model, tools and per-call caps.

Run in a fresh Python process per case and arm, with the chosen checkout on PYTHONPATH.
The CLI accepts a standard Google credential in its environment. Ground truth and
output logs stay outside the temporary project that the DSH session inspects.
"""
import argparse
import json
import os
import tempfile
from pathlib import Path

import yaml


# The oracle distinguishes fixing the interpretation of saved results from
# purchasing new observations. Positive and negative controls guard against
# a reviewer that succeeds by always asking for more work.
CASES = {
    "accuracy": (
        "For this fixed test set only, what is the relative increase in accuracy over the baseline?",
        {"same_test_set": True, "total": 200, "correct": {"baseline": 120, "variant": 150}},
        "Accuracy increased by 15% relative to the baseline, from 60% to 75%.",
        "repair"),
    "rate": (
        "For this one archived transfer, how much higher was the byte rate, relative to the reference?",
        {"same_payload_bytes": 400000, "elapsed_ms": {"reference": 20, "candidate": 16}},
        "At the same payload size, the candidate's byte rate increased by 20% relative to the reference.",
        "repair"),
    "null": (
        "Did the rewrite change the deterministic output on the four specified test vectors?",
        {"vector_ids": [1, 2, 3, 4], "reference": [9, 2, 7, 19], "candidate": [9, 2, 7, 19]},
        "Outputs were identical on all four specified vectors. Untested inputs are outside scope.",
        "write"),
    "gain": (
        "What is the observed proportional change in bytes for exactly these encoded bytes on this fixture?",
        {"same_input": True, "size_bytes": {"reference": 320, "variant": 240}},
        "The variant used 25% fewer bytes on this fixture. This is a descriptive result for this input.",
        "write"),
    "unobserved": (
        "Estimate the candidate's speedup over the reference on GPU B for the same input. "
        "The decision requires an on-device comparison on GPU B.",
        {"same_input": True, "GPU_A": {"reference_ms": 9}, "GPU_B": {"candidate_ms": 6}},
        "Only the candidate was measured on GPU B. A same-device speedup has not been estimated.",
        "collect"),
    "percentile": (
        "Estimate p99 request latency on this service. The decision requires an empirical request-latency distribution.",
        {"captured_fields": ["elapsed_seconds", "completed"], "elapsed_seconds": 80, "completed": 400},
        "Five requests per second completed on average. Per-request timestamps were not retained.",
        "collect"),
    "transport": (
        "How many additional records per dollar were observed in these two completed archive conversions?",
        {"reference": {"records": 1200, "cost_usd": 40}, "variant": {"records": 900, "cost_usd": 15}},
        "The candidate yielded 60 additional records per dollar relative to the reference.",
        "repair"),
    "order": (
        "Did the migration preserve the ordering of all three pairs in this archived trace?",
        {"pair_ids": ["p1", "p2", "p3"], "before": ["A<B", "Z>A", "M<N"],
         "after": ["A<B", "Z>A", "M<N"]},
        "All three pair orderings were preserved on the archived trace. The study makes no transfer claim.",
        "write"),
    "power": (
        "Which device consumes fewer joules for one completed inference on the fixed input?",
        {"reference": {"mean_watts": 40}, "variant": {"mean_watts": 32},
         "logged_fields": ["mean_watts"], "timing_trace_available": False},
        "Average power draw was recorded for both devices. Energy per inference has not been established.",
        "collect"),
}


def make_project(root, idea, data, finding):
    state = root / "auto_research/state"
    state.mkdir(parents=True)
    results = root / "results"
    results.mkdir()
    (results / "measurements.json").write_text(json.dumps(data, indent=2))
    (state / "experiment_plan.yaml").write_text(yaml.safe_dump({"question": idea, "venue": "short empirical note"}))
    (state / "project_context.md").write_text("Local computational study. All required systems were available.")
    (state / "findings.yaml").write_text(yaml.safe_dump({"findings": [{
        "id": "r1", "result": finding, "source": "results/measurements.json",
        "construct": "Descriptive comparison on the stated evaluation scope.",
    }]}))


class ReviewProject:
    def __init__(self, root, model):
        self.code_dir = root
        self.model = model
        self.turns = []
        self._terminal_error = None

    def log_step_header(self, *args):
        pass

    def log(self, *args):
        pass

    log_step = log

    def run_agent(self, role, prompt, *, continue_session=None, session_out=None, outcome_out=None, **kwargs):
        from ark.engines.dsh_runtime import DSHRuntime
        runtime = DSHRuntime(self.model, resume_session_id=continue_session, next_turn=bool(continue_session))
        runtime.agent_type = role
        rc, out, _, elapsed, timed = runtime.execute(
            prompt, "Inspect only the files in this working directory. This is a read-only study review. "
            "Do not write files, launch experiments or submit jobs.", self.code_dir, 110)
        parsed = runtime.parse_output(out)
        result = next((e for e in (json.loads(s) for s in out.splitlines() if s.startswith("{"))
                                   if e.get("kind") == "__result__"), {})
        ok = rc == 0 and not timed and not parsed.get("error_code")
        self.turns.append({"seconds": elapsed, "usage": parsed.get("usage"), "tools": result.get("tool_calls", 0),
                           "error": parsed.get("error_code"), "reply": parsed.get("result")})
        if session_out is not None:
            session_out.append(runtime.session_id)
        if outcome_out is not None:
            outcome_out["completed"] = ok
        if not ok:
            self._terminal_error = "incomplete DSH turn"
        return parsed.get("result") or ""


def run(case, arm, model):
    from ark.engines.dsh_runtime import route
    from ark.pipeline import PipelineMixin
    from ark.quality_gate import extract_json
    if route(model) != ("google-generative-ai", "https://generativelanguage.googleapis.com/v1beta"):
        raise ValueError("This comparison requires a direct native Google model.")
    idea, data, finding, expected = CASES[case]
    with tempfile.TemporaryDirectory(prefix="idea2paper-decision-") as tmp:
        root = Path(tmp)
        make_project(root, idea, data, finding)
        original = {p: p.read_bytes() for folder in ("auto_research", "results")
                    for p in (root / folder).rglob("*") if p.is_file()}
        project = ReviewProject(root, model)
        if arm == "generic":
            reply = project.run_agent("planner", f"Research question: {idea}\nClaim: {finding}\n"
                "Inspect the project plan, findings and underlying results. What is the scientifically appropriate "
                "next action? Choose write if the evidence supports the stated claim, repair if it can be corrected "
                "from the saved evidence, or collect if a new measurement is necessary to answer the question. "
                'Return JSON with "action" and "reason".')
            action = (extract_json(reply, want=dict) or {}).get("action")
        else:
            decision = PipelineMixin._evaluate_completeness(project, idea, finding)
            if isinstance(decision, bool):
                action = "write" if decision else "collect"
            else:
                action = {"proceed_to_writing": "write", "correct_findings": "repair",
                          "reanalyze_existing": "repair", "need_more_experiments": "collect"}.get(
                              getattr(decision, "action", None))
        after = {p: p.read_bytes() for folder in ("auto_research", "results")
                 for p in (root / folder).rglob("*") if p.is_file()}
        if project._terminal_error or original != after:
            action = "incomplete"
        return {"case": case, "arm": arm, "model": model, "expected": expected, "action": action,
                "correct": action == expected, "turns": project.turns}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--arm", choices=("generic", "release", "candidate"), required=True)
    parser.add_argument("--model", default="gemini/gemini-4-hydrogen")
    parser.add_argument("--max-tokens", type=int, default=1800)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    os.environ.update(ARK_DSH_MAX_TOKENS=str(args.max_tokens), ARK_DSH_RETRIES="0", LITELLM_LOCAL_MODEL_COST_MAP="True")
    result = run(args.case, args.arm, args.model)
    result["max_tokens"] = args.max_tokens
    args.out.write_text(json.dumps(result, indent=2))
