#!/usr/bin/env python3
"""Create a standalone cheap-test project, as the webapp's Test preset does.

For end-to-end runtime comparisons on a machine without the webapp or its
database: a 2-page ACM paper on the canned test idea, the canned Deep Research
report and bibliography (no live research call), matplotlib figures only, one
dev iteration and one review iteration, experiments on the local machine.

  python scripts/e2e_project.py --dir ~/ark-eval/E4/dsh --model openai/ext/deepseek-v4.1-flash
  ARK_AGENT_RUNTIME=dsh python -m ark.orchestrator --project e4-dsh \
      --project-dir ~/ark-eval/E4/dsh --code-dir ~/ark-eval/E4/dsh --model <same model>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--venue-format", default="euromlsys")
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--iterations", type=int, default=1)
    a = ap.parse_args()

    from website.dashboard.routes import _substitute_agent_templates
    from website.dashboard.templates import copy_test_fixtures, copy_venue_template, read_test_idea
    root = Path(a.dir).expanduser().resolve()
    if (root / "config.yaml").exists():
        sys.exit(f"{root} already holds a project")
    (root / "paper" / "figures").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    if not copy_venue_template(a.venue_format, root / "paper"):
        sys.exit(f"no bundled template for {a.venue_format}")
    copy_test_fixtures(root, seed_deep_research=True, seed_figures=False)
    name = a.name or root.name
    idea = read_test_idea()
    config = {
        "project": name, "title": "Test run", "idea": idea,
        "venue": "ACM SIGPLAN", "venue_format": a.venue_format, "venue_pages": a.pages,
        "layout_mode": "balanced", "mode": "paper", "model": a.model, "model_variant": "",
        "max_iterations": a.iterations, "max_dev_iterations": 1, "language": "en",
        "code_dir": str(root), "latex_dir": "paper", "figures_dir": "paper/figures",
        "figure_generation": "none", "skip_deep_research": True,
        "orchestrator_compute_backend": {"type": "local"},
        "experiment_compute_backend": {"type": "local"},
        "compute_backend": {"type": "local"},
        "base_conda_env": "ark-base",
    }
    (root / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    (root / "auto_research" / "state").mkdir(parents=True, exist_ok=True)
    (root / "auto_research" / "state" / "idea.md").write_text(idea)
    # The agents read their role prompt from <project>/agents; the orchestrator
    # does not create it, the create/restart flows do.
    _substitute_agent_templates(root, name, "Test run", "ACM SIGPLAN",
                                a.venue_format, a.pages)
    print(f"project ready: {root}")


if __name__ == "__main__":
    main()
