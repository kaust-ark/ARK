#!/usr/bin/env python3
"""One agent call through ARK's real run_agent, on a fixed task, with a checker.

Compares agent runtimes (OpenHands CLI vs DSH) on the same model and the same
role prompt. Each case builds a small project, runs one role, and checks the
result against facts the agent cannot fake (recomputed numbers, unchanged
tables). Usage is reported in one normalized form: OpenHands counts cached
input inside input_tokens, DSH does not.

  python scripts/runtime_case.py --case experiment --runtime dsh \
      --model deepseek/ext/deepseek-v4.1-flash --out evals/E2 --timeout 1200 \
      --price-in 0.137 --price-cache 0.00274 --price-out 0.548
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

N = 200


def _values():
    rng = random.Random(20261105)
    return [round(rng.gauss(10.0, 2.5), 4) for _ in range(N)]


TEX = r"""\documentclass{article}
\usepackage{graphicx}
\begin{document}
\title{Stability of a Toy Estimator}
\maketitle
\begin{abstract}
We study a toy estimator. It is simple. We report results.
\end{abstract}
\section{Introduction}
Estimators matter. This paper looks at one of them on synthetic data.
\section{Results}
\begin{table}[t]
\centering
\begin{tabular}{lrr}
Setting & Mean & Std \\
A & 9.912 & 2.481 \\
B & 10.207 & 2.533 \\
\end{tabular}
\caption{Main results.}\label{tab:main}
\end{table}
\begin{figure}[t]
\centering
\includegraphics[width=0.5\linewidth]{figures/plot.pdf}
\caption{Distribution.}\label{fig:dist}
\end{figure}
\section{Conclusion}
The estimator is stable across settings A and B (Table~\ref{tab:main}).
\end{document}
"""


def build(case: str, root: Path) -> tuple[str, str, int]:
    """Create the project; return (role, task, default timeout)."""
    (root / "auto_research" / "state").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "data" / "values.csv").write_text("value\n" + "\n".join(map(str, _values())) + "\n")
    if case == "experiment":
        return "experimenter", (
            "Write scripts/analyze.py that reads data/values.csv and writes results/metrics.json "
            "with the keys `n`, `mean` and `std` (population standard deviation). Run it, check "
            "the output file, and report the three numbers."), 1200
    if case == "longjob":
        (root / "scripts").mkdir()
        slow = root / "scripts" / "slow_step.sh"
        slow.write_text("#!/bin/bash\n# Stands in for a long experiment.\nsleep 150\n"
                        "echo ok > results/slow_step.done\n")
        slow.chmod(0o755)
        return "experimenter", (
            "Run scripts/slow_step.sh first (it takes about two and a half minutes and writes "
            "results/slow_step.done; create results/ before starting it). After it has finished, "
            "write scripts/analyze.py that reads data/values.csv and writes results/metrics.json "
            "with the keys `n`, `mean` and `std` (population standard deviation), run it, and "
            "report the three numbers."), 1800
    if case == "prose":
        (root / "paper" / "figures").mkdir(parents=True)
        (root / "paper" / "main.tex").write_text(TEX)
        return "writer", (
            "Rewrite the abstract of paper/main.tex so that it states the sample size (n = 200 per "
            "setting) and the main finding (the mean stays within 0.3 of 10 in both settings) in "
            "at most three sentences. Change nothing outside the abstract: no table, figure, "
            "section or number elsewhere may change. Do not compile."), 1200
    raise SystemExit(f"unknown case {case}")


def check(case: str, root: Path) -> dict:
    out = {}
    if case in ("experiment", "longjob"):
        vals = _values()
        want = {"n": N, "mean": statistics.fmean(vals), "std": statistics.pstdev(vals)}
        try:
            got = json.loads((root / "results" / "metrics.json").read_text())
            out["metrics_correct"] = all(abs(float(got[k]) - v) <= 1e-6 * max(1, abs(v))
                                         for k, v in want.items())
        except Exception as e:
            out["metrics_correct"] = False
            out["metrics_error"] = f"{type(e).__name__}: {e}"
        out["script_exists"] = (root / "scripts" / "analyze.py").exists()
        if case == "longjob":
            out["slow_step_done"] = (root / "results" / "slow_step.done").exists()
    else:
        before, after = TEX, (root / "paper" / "main.tex").read_text()
        abstract = lambda t: re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", t, re.S)
        outside = lambda t: re.sub(r"\\begin\{abstract\}.*?\\end\{abstract\}", "", t, flags=re.S)
        a = abstract(after)
        out["abstract_changed"] = bool(a) and a.group(1).strip() != abstract(before).group(1).strip()
        out["abstract_mentions_200"] = bool(a) and "200" in a.group(1)
        out["outside_abstract_unchanged"] = outside(after) == outside(before)
    out["pass"] = all(v for k, v in out.items() if isinstance(v, bool))
    return out


def normalized(stats: list, runtime: str) -> dict:
    tot = {"requests": 0, "uncached_input": 0, "cache_read": 0, "output": 0, "estimated": False}
    for r in stats:
        cache = int(r.get("cache_read_tokens") or 0)
        inp = int(r.get("input_tokens") or 0)
        tot["requests"] += int(r.get("requests") or 0)
        tot["cache_read"] += cache
        # OpenHands' input includes the cached part; DSH's does not.
        tot["uncached_input"] += max(0, inp - cache) if runtime == "openhands" else inp
        tot["output"] += int(r.get("output_tokens") or 0)
        tot["estimated"] |= bool(r.get("estimated"))
    return tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True, choices=["experiment", "longjob", "prose"])
    ap.add_argument("--runtime", required=True, choices=["dsh", "openhands"])
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=int, default=0)
    ap.add_argument("--price-in", type=float, default=0.0, help="USD per 1M uncached input tokens")
    ap.add_argument("--price-cache", type=float, default=0.0, help="USD per 1M cache-read tokens")
    ap.add_argument("--price-out", type=float, default=0.0, help="USD per 1M output tokens")
    ap.add_argument("--stop-after", type=float, default=0.0,
                    help="send the run a Stop (SIGTERM, as the dashboard does) after this many seconds")
    a = ap.parse_args()

    if a.runtime == "dsh":
        os.environ["ARK_AGENT_RUNTIME"] = "dsh"
    else:
        os.environ.pop("ARK_AGENT_RUNTIME", None)
    import ark
    import ark.engines as eng

    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    run = Path(a.out).resolve() / f"{a.case}-{a.runtime}-{stamp}"
    root = run / "project"
    root.mkdir(parents=True)
    role, task, default_timeout = build(a.case, root)
    timeout = a.timeout or default_timeout
    lines = []

    class Harness(eng.AgentMixin):
        pass

    h = Harness()
    for name, val in dict(
            agents_dir=REPO / "ark" / "templates" / "agents", state_dir=root / "auto_research" / "state",
            project_name="runtime-case", config={},
            code_dir=root, log_dir=run / "logs", model=a.model,
            memory=SimpleNamespace(get_context_for_agent=lambda r: ""),
            _agent_stats=[], _agent_empty_count=0, _rate_limit_notified=False,
            _terminal_error=None, _quota_exhausted=False,
            log=lambda m, l="INFO": lines.append(f"[{time.strftime('%H:%M:%S')}] {l} {m}"),
            log_step=lambda m, *x, **k: lines.append(f"[{time.strftime('%H:%M:%S')}] STEP {m}"),
            log_summary_box=lambda *x, **k: None, send_notification=lambda *x, **k: None,
            save_checkpoint=lambda *x, **k: None, _write_cost_report=lambda *x, **k: None,
            # On a signal exit run_agent "cleans up" with pkill -f on claude
            # processes, a leftover from the Claude CLI era. Never on a shared
            # workstation.
            _cleanup_cli_state=lambda *x, **k: None,
            _summarize_agent_output=lambda r, t: []).items():
        setattr(h, name, val)
    # As in production: the agent's HOME and TMPDIR are the project. The DSH
    # runtime's unpacked modules stay in one shared cache.
    os.environ.setdefault("ARK_DSH_PKG_CACHE", str(Path.home() / ".cache" / "idea2paper" / "dsh-pkg"))
    os.environ["HOME"] = os.environ["TMPDIR"] = str(root)
    os.chdir(root)
    stopped_at = None
    if a.stop_after:
        import signal
        import threading

        def on_stop(sig, frame):          # what the orchestrator's handler does
            ark.shutting_down.set()
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, on_stop)
        threading.Timer(a.stop_after, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    t0 = time.time()
    try:
        answer = h.run_agent(role, task, timeout=timeout)
        crashed = None
    except SystemExit:
        answer, crashed, stopped_at = "", None, round(time.time() - t0, 1)
    except Exception as e:
        answer, crashed = "", f"{type(e).__name__}: {e}"
    wall = round(time.time() - t0, 1)
    leftovers = []
    if a.stop_after:
        time.sleep(10)                     # the grace period, then look
        import subprocess as sp
        ps = sp.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout.splitlines()
        leftovers = [l.strip()[:160] for l in ps if str(root) in l and str(os.getpid()) not in l.split()[0]]
    usage = normalized(h._agent_stats, a.runtime)
    cost = (usage["uncached_input"] * a.price_in + usage["cache_read"] * a.price_cache
            + usage["output"] * a.price_out) / 1e6
    report = {"case": a.case, "runtime": a.runtime, "model": a.model, "role": role,
              "timeout": timeout, "wall_s": wall, "answer": (answer or "")[:600],
              "terminal_error": h._terminal_error, "crashed": crashed,
              "stopped_at_s": stopped_at, "processes_left_after_stop": leftovers,
              "checks": check(a.case, root), "usage": usage, "cost_usd": round(cost, 5),
              "calls": [{k: s.get(k) for k in ("requests", "input_tokens", "cache_read_tokens",
                                                "output_tokens", "estimated", "cost_usd")}
                        for s in h._agent_stats]}
    (run / "report.json").write_text(json.dumps(report, indent=1))
    (run / "harness.log").write_text("\n".join(lines) + "\n")
    print(json.dumps({k: report[k] for k in ("case", "runtime", "wall_s", "terminal_error", "crashed",
                                             "stopped_at_s", "processes_left_after_stop",
                                             "checks", "usage", "cost_usd")}, indent=1))


if __name__ == "__main__":
    main()
