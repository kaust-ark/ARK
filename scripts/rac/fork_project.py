"""Fork a finished ARK project into a matched pair of RAC arms.

Both arms start from the *same* bytes — the research phase's output and nothing
else — so the only difference between them is who decides what happens next:

    arm A (control)  no `sharednet:` block  → ARK's fixed loop
                     _run_dev_phase() then run_paper_iteration() × max_iterations
    arm B (RAC)      `sharednet:` block     → the SharedNet Room loop
                     run_room_team(): the Agent that just finished says who is next

What is kept is the research phase's product (idea, project context, deep
research, selected skills, per-role knowledge) plus anything downloaded rather
than reasoned (data/, code/, scripts/ — datasets and model caches live there).
What is removed is every product of the dev phase and the review loop, which is
exactly the work the two arms are being asked to redo.

    python scripts/rac/fork_project.py \
        --source .../projects/ark_eval_v6_dl4c --name rac_dl4c \
        --invite "ROOM=… TOKEN=… BASE=…" [--max-hops 10] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import shutil
import subprocess
import sys

import yaml

# Written by the research phase, before the "Dev Phase" section of the log.
# Verified against the mtimes of ark_eval_v6_{dl4c,scsl,bi_align}: every one of
# these predates that project's dev_phase_state.yaml.
RESEARCH_STATE_KEEP = {
    "idea.md",
    "project_context.md",
    "deep_research.md",
    "deep_research_assets",
    "ethical_review.json",
    "user_prefs.yaml",
    "selected_skills.json",
    "selected_skills_rationale.md",
}
KEEP_SUFFIXES = ("_knowledge.md",)          # experimenter_knowledge.md, planner_…, …

# Products of the dev phase / review loop. Removed wholesale.
DROP_DIRS = ("paper", "results")
# Inputs, not products: datasets and model caches. Deleting them buys nothing and
# costs hours of re-download, and both arms get the identical copy.
KEEP_DIRS = ("data", "code", "scripts")


def keep_state_file(path: pathlib.Path) -> bool:
    return path.name in RESEARCH_STATE_KEEP or path.name.endswith(KEEP_SUFFIXES)


def reset_to_research(root: pathlib.Path, dry: bool, log) -> dict:
    """Strip an arm back to the research phase. Returns what was kept/dropped."""
    kept, dropped = [], []
    state = root / "auto_research" / "state"
    if state.is_dir():
        for item in sorted(state.iterdir()):
            if keep_state_file(item):
                kept.append(f"state/{item.name}")
                continue
            dropped.append(f"state/{item.name}")
            if not dry:
                shutil.rmtree(item) if item.is_dir() else item.unlink()
    for name in DROP_DIRS:
        target = root / name
        if target.exists():
            dropped.append(f"{name}/")
            if not dry:
                shutil.rmtree(target)
    # A copied conda env is a broken conda env: every shebang and every path in
    # conda-meta still points at the source project, and `project_env_ready`
    # only checks that conda-meta exists — so the launcher would pick it and
    # every agent call would run against the wrong interpreter. Drop it and let
    # the launcher fall back to the interpreter that started the run.
    for name in (".conda_env", ".pid", ".env_provision.log"):
        target = root / name
        if target.exists():
            dropped.append(name)
            if not dry:
                shutil.rmtree(target) if target.is_dir() else target.unlink()
    for name in ("logs", "auto_research/logs"):
        target = root / name
        if target.exists():
            dropped.append(f"{name}/")
            if not dry:
                shutil.rmtree(target)
                target.mkdir(parents=True, exist_ok=True)
    for name in KEEP_DIRS:
        if (root / name).exists():
            kept.append(f"{name}/")
    for line in [f"  kept:    {', '.join(kept) or '(nothing)'}",
                 f"  dropped: {', '.join(dropped) or '(nothing)'}"]:
        log(line)
    return {"kept": kept, "dropped": dropped}


def write_config(root: pathlib.Path, arm: str, args, dry: bool, log) -> dict:
    config = yaml.safe_load((root / "config.yaml").read_text()) or {}
    config["code_dir"] = str(root)
    config["max_dev_iterations"] = args.max_dev_iterations
    config["figure_generation"] = "nano_banana"     # the arms must be able to draw
    config.pop("sharednet", None)

    if arm == "b":
        config["sharednet"] = {
            "invite": args.invite,
            "max_hops": args.max_hops,
            # Without this the Room starts at `writer` (no reviews on file yet)
            # while arm A starts at compile→review: an extra free hop for B.
            "start_role": args.start_role,
            "roles": list(args.roles),
        }
    else:
        config["max_iterations"] = args.max_iterations

    if not dry:
        (root / "config.yaml").write_text(yaml.dump(config, default_flow_style=False,
                                                    allow_unicode=True, sort_keys=False))
    log(f"  config:  max_dev_iterations={args.max_dev_iterations}, "
        + (f"sharednet.max_hops={args.max_hops}, start_role={args.start_role}"
           if arm == "b" else f"max_iterations={args.max_iterations}"))
    return config


def git_init(root: pathlib.Path, dry: bool, log) -> None:
    """ARK commits once per iteration and `_should_skip_figure_phase` reads
    `git diff HEAD~1`; give it a repo with a base commit to diff against."""
    if dry:
        log("  git:     (dry run)")
        return
    if (root / ".git").exists():
        shutil.rmtree(root / ".git")
    for cmd in (["init", "-q"], ["add", "-A"],
                ["-c", "user.email=rac@local", "-c", "user.name=RAC fork",
                 "commit", "-q", "-m", "RAC fork point: research phase output only"]):
        subprocess.run(["git", "-C", str(root)] + cmd, check=False,
                       capture_output=True, text=True)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    log(f"  git:     base commit {head}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, help="a finished ARK project directory")
    parser.add_argument("--name", required=True, help="base name; arms become <name>_a / <name>_b")
    parser.add_argument("--dest-root", default=str(pathlib.Path(__file__).resolve().parents[2] / "projects"))
    parser.add_argument("--invite", default="", help="SharedNet invite for arm B")
    parser.add_argument("--max-hops", type=int, default=10)
    parser.add_argument("--max-iterations", type=int, default=2, help="arm A review rounds")
    parser.add_argument("--max-dev-iterations", type=int, default=1)
    parser.add_argument("--start-role", default="experimenter",
                    help="first role to act; at the research fork point the protocol "
                         "exists but no experiment has run, which is the experimenter's cue")
    parser.add_argument("--roles", nargs="+",
                        default=["experimenter", "coder", "writer", "reviewer", "planner"])
    parser.add_argument("--arms", nargs="+", default=["a", "b"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = pathlib.Path(args.source).resolve()
    if not (source / "config.yaml").exists():
        print(f"error: {source} is not an ARK project (no config.yaml)", file=sys.stderr)
        return 1
    if "b" in args.arms and not args.invite and not args.dry_run:
        print("error: arm B needs --invite (start scripts/rac/dev_room.py first)", file=sys.stderr)
        return 1

    dest_root = pathlib.Path(args.dest_root).resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    manifest = {"source": str(source), "forked_at": datetime.datetime.now().isoformat(), "arms": {}}

    for arm in args.arms:
        root = dest_root / f"{args.name}_{arm}"
        print(f"\n── arm {arm.upper()}  →  {root}")
        if root.exists():
            print(f"  error: {root} already exists; remove it or pick another --name", file=sys.stderr)
            return 1
        if not args.dry_run:
            shutil.copytree(source, root, symlinks=True, ignore=shutil.ignore_patterns(".git"))
            # `workspace` is a self-symlink in ARK projects; re-point it at the arm.
            link = root / "workspace"
            if link.is_symlink():
                link.unlink()
                link.symlink_to(root)
        log = print
        detail = reset_to_research(root, args.dry_run, log) if not args.dry_run else \
            reset_to_research(source, True, log)
        config = write_config(root, arm, args, args.dry_run, log) if not args.dry_run else {}
        git_init(root, args.dry_run, log)
        manifest["arms"][arm] = {"path": str(root), **detail,
                                 "loop": "sharednet Room" if arm == "b" else "fixed review loop"}
        if not args.dry_run:
            (root / "auto_research" / "state" / "rac_fork.yaml").write_text(
                yaml.dump({**manifest, "this_arm": arm}, default_flow_style=False, allow_unicode=True))

    print(f"\nforked {len(args.arms)} arm(s) from {source.name}"
          + ("  (dry run, nothing written)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
