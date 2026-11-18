"""Replay the production draft prompt on fixed evidence with a free OpenRouter model.

Run this same script twice, changing --checkout and --out. Each case makes one
completion, and saves the source prompt, returned LaTeX, and provider metadata.
Read the Results and Limitations together against the raw measurements.

To compare treatment of replication, select --cases null repeats aggregate timing.
In null, rows index different sizes and establish only agreement between methods.
In repeats, three restarts per load support the observed 5–7 s and 8–10 s ranges.
In aggregate, the stored summaries support the reported sample SDs of 2 W and 4 W.
The pairing and independence of the runs are unknown. In timing, a single timing
per method supports the observed difference and leaves run-to-run spread unknown.
"""
import argparse
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

import yaml


CASES = {
    "timing": (
        "Does algorithm B run faster on this workload?",
        {"times_ms": {"A": [2.12], "B": [1.89]}, "host_count": 1},
        "One run took 2.12 ms for A and 1.89 ms for B.",
        "Only one timed run per method on one host. No warm-up or repeated measurements.",
    ),
    "baseline": (
        "Does adding the feature improve classification over a tuned baseline?",
        {"correct": 84, "total": 100, "feature": "enabled", "baseline_trials": 0},
        "The enabled feature classified 84 of 100 examples correctly.",
        "Baseline not evaluated. No paired estimate of improvement.",
    ),
    "service": (
        "What do the measurements establish about serving throughput and tail latency?",
        {"requests_completed": 1200, "interval_seconds": 60, "per_request_latency_samples": 0},
        "The service completed 1200 requests during one 60-second interval.",
        "Per-request durations were not recorded. The percentile latency is unknown.",
    ),
    "null": (
        "Does a rewrite reduce comparisons for missing keys?",
        {"comparisons": {"reference": [4, 8, 16], "rewrite": [4, 8, 16]}, "sizes": [4, 8, 16]},
        "Both implementations used 4, 8, and 16 comparisons on the three tested sizes.",
        "Present keys and other input sizes were not measured.",
    ),
    "repeats": (
        "How does completion time vary across restarts at the tested loads?",
        {"unit": "seconds", "independent_restarts": True,
         "by_load": {"low": {"a": 7, "b": 5, "c": 6}, "high": {"a": 8, "b": 9, "c": 10}}},
        "The three restart times at low load were 7, 5, and 6 seconds. "
        "Those at high load were 8, 9, and 10 seconds.",
        "The two loads were tested on one machine.",
    ),
    "aggregate": (
        "What variation in power draw was observed at the two settings?",
        {"unit": "watts", "settings": {"idle": {"n_runs": 3, "mean": 20, "sample_sd": 2},
                                       "busy": {"n_runs": 3, "mean": 60, "sample_sd": 4}}},
        "Mean power was 20 W at idle and 60 W when busy.",
        "The recorder stored per-setting summary statistics only. Individual readings are unavailable.",
    ),
}

DEFAULT_CASES = ("timing", "baseline", "service", "null")


def capture_prompt(root, idea, result, gap):
    from ark.orchestrator.state import StateManager
    from ark.pipeline import PipelineMixin

    state = root / "auto_research/state"
    state.mkdir(parents=True)
    record = {"coverage": {"experiment_1": {"status": "done", "scope": idea}},
              "findings": [{"id": "f1", "result": result, "source": "results/test.json",
                            "construct": "Measured values for this sample."}]}
    (state / "findings.yaml").write_text(yaml.safe_dump(record))
    (state / "dev_phase_state.yaml").write_text(yaml.safe_dump({
        "iteration": 1, "status": "in_progress", "sufficient": False, "evidence_gaps": [gap],
        "steps_done": ["plan", "run", "analyze", "evaluate"],
    }))
    writer = PipelineMixin()
    writer.code_dir, writer.state_dir = root, state
    writer.config = {"venue_pages": 2}
    writer._list_available_figures = lambda: "None"
    writer.load_paper_requirements = lambda: {}
    writer._load_findings_summary = StateManager(state).load_findings_summary
    prompts = []
    writer.run_agent = lambda role, prompt, **kwargs: prompts.append(prompt)
    writer._write_initial_draft(idea)
    return prompts[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--model", default="nvidia/nemotron-3-ultra-550b-a55b:free")
    parser.add_argument("--cases", nargs="+", choices=CASES, default=DEFAULT_CASES,
                        help="Replay the selected fixed cases, one model request each.")
    args = parser.parse_args()
    sys.path.insert(0, str(args.checkout.resolve()))
    key = os.environ["OPENROUTER_API_KEY"]
    with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=30) as response:
        models = json.load(response)["data"]
    if not any(m["id"] == args.model and m["pricing"] and all(float(v) == 0 for v in m["pricing"].values())
               for m in models):
        parser.error("Select a model listed with zero pricing for every component.")
    args.out.mkdir(parents=True, exist_ok=False)
    system = ("You are writing the Results and Limitations excerpt for this manuscript. Output just those "
              "two sections of LaTeX body, at most 150 words. All relevant raw measurements follow, shared "
              "verbatim across evaluations. You have no file or shell tools. Do not supply a preamble, "
              "other sections or references.")
    for case in args.cases:
        idea, raw, result, gap = CASES[case]
        with tempfile.TemporaryDirectory() as tmp:
            prompt = capture_prompt(Path(tmp), idea, result, gap)
        user = prompt + "\n\nAvailable file results/test.json\n" + json.dumps(raw)
        body = {"model": args.model, "seed": 42, "temperature": 0, "max_tokens": 600,
                "provider": {"allow_fallbacks": False, "max_price": {"prompt": 0, "completion": 0}},
                "reasoning": {"enabled": False},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        (args.out / f"{case}-request.json").write_text(json.dumps(body, indent=2))
        req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as response:
            reply = json.load(response)
        (args.out / f"{case}-response.json").write_text(json.dumps(reply, indent=2))
        choice = reply["choices"][0]
        if choice["finish_reason"] != "stop" or not choice["message"].get("content"):
            raise RuntimeError(f"{case} has an incomplete response")
        (args.out / f"{case}.tex").write_text(choice["message"]["content"])
        print(case, reply.get("model"), reply.get("provider"), reply.get("usage", {}).get("cost"), flush=True)


if __name__ == "__main__":
    main()
