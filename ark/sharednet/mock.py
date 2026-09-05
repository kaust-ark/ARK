"""A scripted research team, for demonstrating the Room loop in seconds.

No model, no OpenHands, no LaTeX: each role returns a plausible message and a
``HANDOFF`` line. The route is decided by those lines, exactly as it would be
by real Agents, so the Room transcript produced here has the same shape as a
real run's.
"""

from __future__ import annotations

import time
from typing import Callable

STORY: dict[str, list[str]] = {
    "experimenter": [
        "Ran the prefetch-aware tiering sweep on 3 KV-cache traces (2 GPU nodes, 40 min).\n"
        "Recorded p50/p99 latency and hit rate for 4 tier policies in findings.yaml; "
        "coverage: 3/4 protocol items (the mixed-workload trace is still running).\n"
        'HANDOFF: {"next": "writer", "done": false, "reason": "enough numbers for a first full draft; mixed-workload results can be added in revision"}',
        "Finished the mixed-workload trace and the missing ablation on prefetch window size "
        "(4 settings). findings.yaml coverage is now 4/4; the ablation table is in results/ablation.csv.\n"
        'HANDOFF: {"next": "writer", "done": false, "reason": "ablation numbers are in; §5.3 can be written from results/ablation.csv"}',
    ],
    "writer": [
        "Wrote the full draft in paper/main.tex: abstract, intro with 4 contributions, related work "
        "(12 refs), method, experiments with the main results table, discussion, conclusion. "
        "Compiles to 8.4 body pages.\n"
        'HANDOFF: {"next": "reviewer", "done": false, "reason": "first complete draft compiles; needs a venue-standard review"}',
        "Revised §5: added the ablation subsection and table, tightened the intro claims to what the "
        "numbers support, moved hyper-parameter details to the appendix. 7.9 body pages, compiles.\n"
        'HANDOFF: {"next": "reviewer", "done": false, "reason": "both Major issues addressed; ready for re-review"}',
    ],
    "reviewer": [
        "# Review\n\nOverall Score: 6.5/10\n\n## Major Issues\n"
        "### M1. Missing ablation on the prefetch window\nThe central design choice is asserted, never isolated.\n"
        "### M2. Over-claiming in the introduction\n\"Eliminates\" tail latency is not supported by the p99 numbers.\n\n"
        "## Minor Issues\n### m1. Figure 3 axis labels unreadable\n"
        'HANDOFF: {"next": "planner", "done": false, "reason": "two Major issues, one needs new data; the planner should sequence experiment before writing"}',
        "# Review\n\nOverall Score: 8.5/10\n\nM1 resolved by the new ablation (Table 4). M2 resolved: claims now "
        "match the p99 results. One minor: caption of Table 4 could state N.\n"
        'HANDOFF: {"next": null, "done": true, "reason": "8.5/10, above the 8.0 threshold; remaining issue is cosmetic"}',
    ],
    "planner": [
        "Action plan written (auto_research/state/action_plan.yaml):\n"
        "- M1 → EXPERIMENT_REQUIRED (ablation over prefetch window; 4 settings; results/ablation.csv)\n"
        "- M2 → WRITING_ONLY (rewrite intro claims to match p99 numbers)\n"
        "- m1 → FIGURE_CODE_REQUIRED (font size in scripts/create_paper_figures.py)\n"
        'HANDOFF: {"next": "experimenter", "done": false, "reason": "M1 needs data before the writer can revise §5"}',
    ],
}


def scripted_team(delay_seconds: float = 0.0, story: dict[str, list[str]] | None = None,
                  already_played: dict[str, int] | None = None) -> Callable[[str, str], str]:
    """A ``run_agent`` that plays the story above, one line per call.

    ``already_played`` (role → count) skips lines a previous process already
    said in this Room, so a resumed demo continues the story instead of
    repeating it.
    """
    remaining = {role: list(lines)[(already_played or {}).get(role, 0):]
                 for role, lines in (story or STORY).items()}

    def run_agent(role: str, task: str) -> str:
        if delay_seconds:
            time.sleep(delay_seconds)
        lines = remaining.get(role) or []
        if lines:
            return lines.pop(0)
        return (f"({role}) nothing further to add.\n"
                'HANDOFF: {"next": "reviewer", "done": false, "reason": "no scripted work left; ask the reviewer"}')

    return run_agent
