"""Create a fresh ARK project for one RAC arm, from a finished project's idea.

The control arm is an existing finished run; this builds the treatment arm that
will be compared against it. Everything the two arms share — the research idea,
the venue and page budget, the model, the acceptance threshold, the figure
pipeline — is copied verbatim from that run's config, so the only difference is
the `sharednet:` block that routes ARK through the Room loop.

Nothing is copied from the source *directory*: the research phase, the
experiments and the draft are all produced fresh. That phase is identical code
in both arms (the RAC diff never touches it), so re-running it adds variance,
not bias — and it avoids dragging gigabytes of downloaded datasets across NFS.

    python scripts/rac/new_arm.py --source .../ark_eval_v6_scsl --name rac_scsl_b \
        --invite "ROOM=… TOKEN=… BASE=…" --max-hops 23
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import yaml

# Copied verbatim from the control run so the arms differ only in the loop.
SHARED_KEYS = (
    "venue", "venue_format", "venue_pages", "title", "model", "model_variant",
    "paper_accept_threshold", "latex_dir", "figures_dir", "scripts_dir",
    "create_figures_script", "figure_generation", "nano_banana_model",
    "language", "research_idea", "goal_anchor",
    "orchestrator_compute_backend", "experiment_compute_backend",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="the finished control-arm project")
    ap.add_argument("--name", required=True)
    ap.add_argument("--invite", required=True)
    ap.add_argument("--max-hops", type=int, required=True)
    ap.add_argument("--max-dev-iterations", type=int, default=2)
    ap.add_argument("--start-role", default="experimenter")
    ap.add_argument("--projects-root",
                    default=str(pathlib.Path(__file__).resolve().parents[2] / "projects"))
    args = ap.parse_args()

    src = yaml.safe_load((pathlib.Path(args.source) / "config.yaml").read_text()) or {}
    root = pathlib.Path(args.projects_root).resolve() / args.name
    if root.exists():
        print(f"error: {root} already exists", file=sys.stderr)
        return 1

    config = {k: src[k] for k in SHARED_KEYS if k in src and src[k] is not None}
    config["code_dir"] = str(root)
    config["max_dev_iterations"] = args.max_dev_iterations
    config["sharednet"] = {
        "invite": args.invite,
        "max_hops": args.max_hops,
        "start_role": args.start_role,
        "roles": ["experimenter", "coder", "writer", "reviewer", "planner"],
    }
    # Deep Research needs a Gemini key we do not have; the control run did not
    # get AI concept figures for the same reason, and skipping it keeps the
    # research phase to the part both arms actually ran.
    config["skip_deep_research"] = True

    for sub in ("auto_research/state", "auto_research/logs", "paper/figures", "code",
                "scripts", "results", "data"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(
        yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False))
    link = root / "workspace"
    if not link.exists():
        link.symlink_to(root)

    print(f"  {args.name}")
    print(f"    idea      : {str(config.get('title') or config['research_idea'])[:70]}")
    print(f"    venue     : {config.get('venue')} {config.get('venue_pages')}p  |  "
          f"model {config.get('model')}/{config.get('model_variant')}  |  thr {config.get('paper_accept_threshold')}")
    print(f"    figures   : {config.get('figure_generation')}")
    print(f"    room      : max_hops={args.max_hops}  start={args.start_role}  "
          f"dev_iters={args.max_dev_iterations}")
    print(f"    path      : {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
