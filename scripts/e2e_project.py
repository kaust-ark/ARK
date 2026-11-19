#!/usr/bin/env python3
"""Create a standalone cheap-test project or a fresh manuscript evaluation case.

For end-to-end runtime comparisons on a machine without the webapp or its
database: a 2-page ACM paper on the canned test idea, the canned Deep Research
report and bibliography (no live research call), matplotlib figures only, one
dev iteration and one review iteration, experiments on the local machine.

  python scripts/e2e_project.py --dir ~/ark-eval/E4/dsh --model openai/ext/deepseek-v4.1-flash
  ARK_AGENT_RUNTIME=dsh python -m ark.orchestrator --project e4-dsh \
      --project-dir ~/ark-eval/E4/dsh --code-dir ~/ark-eval/E4/dsh --model <same model>

For a manuscript evaluation, use --manuscript-case digits or friedman together
with --slurm-config <site-backend.yaml>. This copies only the ordinary template
and the research brief. Start the orchestrator with your configured DSH runtime
and Google credentials. Evaluate the resulting project with eval_manuscript.py.
"""
from __future__ import annotations

import argparse
import os
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
    ap.add_argument("--venue-format")
    ap.add_argument("--pages", type=int)
    ap.add_argument("--iterations", type=int, default=1)
    ap.add_argument("--manuscript-case", choices=("digits", "friedman"))
    ap.add_argument("--slurm-config", type=Path, help="YAML experiment backend mapping for the site")
    a = ap.parse_args()
    if a.manuscript_case:
        if not a.slurm_config or a.venue_format not in (None, "icml") or a.pages not in (None, 4):
            ap.error("Manuscript cases use --slurm-config, the ICML template and four body pages.")
        backend = yaml.safe_load(a.slurm_config.read_text())
        if not isinstance(backend, dict) or backend.get("type") != "slurm":
            ap.error("--slurm-config must contain a Slurm backend (type: slurm).")
        venue, title, a.venue_format, a.pages = "ICML 2026", "", "icml", 4
    else:
        if a.slurm_config:
            ap.error("--slurm-config is for manuscript cases.")
        backend = {"type": "local"}
        venue, title = "ACM SIGPLAN", "Test run"
        a.venue_format, a.pages = a.venue_format or "euromlsys", a.pages or 2

    from website.dashboard.routes import _substitute_agent_templates
    from website.dashboard.templates import copy_test_fixtures, copy_venue_template, read_test_idea
    root = Path(a.dir).expanduser().resolve()
    if a.manuscript_case and root.exists() and any(root.iterdir()):
        sys.exit(f"Manuscript evaluation requires a fresh directory: {root}")
    if (root / "config.yaml").exists():
        sys.exit(f"{root} already holds a project")
    (root / "paper" / "figures").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    if not copy_venue_template(a.venue_format, root / "paper"):
        sys.exit(f"no bundled template for {a.venue_format}")
    if a.manuscript_case:
        idea = (REPO / "test_fixtures/manuscript" / f"{a.manuscript_case}.md").read_text()
        title = idea.splitlines()[0]
    else:
        copy_test_fixtures(root, seed_deep_research=True, seed_figures=False)
        idea = read_test_idea()
    name = a.name or root.name
    config = {
        "auto_github_remote": False,
        "project": name, "title": title, "idea": idea,
        "venue": venue, "venue_format": a.venue_format, "venue_pages": a.pages,
        "layout_mode": "balanced", "mode": "paper", "model": a.model, "model_variant": "",
        "max_iterations": a.iterations, "max_dev_iterations": 1, "language": "en",
        "code_dir": str(root), "latex_dir": "paper", "figures_dir": "paper/figures",
        "figure_generation": "none", "skip_deep_research": True,
        "orchestrator_compute_backend": {"type": "local"},
        "experiment_compute_backend": backend,
        "compute_backend": backend,
        "base_conda_env": "ark-base",
    }
    if a.manuscript_case:
        config.update(base_conda_env=os.getenv("PROJECT_BASE_CONDA_ENV", "ark-base"),
                      layout_mode="strict", autonomy_level="full_auto", figure_generation="nano_banana",
                      nano_banana_model="flash", nano_banana_critic_rounds=1,
                      skip_deep_research=False, deep_research_backend="gemini")
    (root / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    (root / "auto_research" / "state").mkdir(parents=True, exist_ok=True)
    (root / "auto_research" / "state" / "idea.md").write_text(idea)
    # The agents read their role prompt from <project>/agents; the orchestrator
    # does not create it, the create/restart flows do.
    _substitute_agent_templates(root, name, title, venue,
                                a.venue_format, a.pages)
    print(f"project ready: {root}")


if __name__ == "__main__":
    main()
