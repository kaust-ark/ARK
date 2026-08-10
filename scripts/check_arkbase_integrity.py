#!/usr/bin/env python3
"""Watchdog: detect dependency drift in the shared base conda env (ark-base).

Why: ark-base is shared platform infrastructure — every orchestrator runs on
it. History shows one stray `pip install` there (vllm 2026-07-10, an agent's
otree 2026-07-18) silently breaks Gate A, PaperBanana, or the control plane
for EVERY user, and the damage can lurk for days. This script pins a blessed
baseline and screams on drift.

Usage:
  check_arkbase_integrity.py --bless      # record current state as baseline
  check_arkbase_integrity.py              # compare + probe, exit 1 on drift
  check_arkbase_integrity.py --notify     # also email the admin on drift

Baseline lives next to the env: <conda_root>/envs/<env>.baseline.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Platform-critical packages: a version change here has historically broken
# (or would break) Gate A / figures / control-plane sync for every run.
CRITICAL_PACKAGES = [
    "sqlalchemy", "sqlmodel", "starlette",
    "litellm", "openai", "anthropic", "httpx",
    "markupsafe", "pymupdf",
]

# Functional probes run INSIDE the target env — they catch breakage that
# version pins can't (e.g. the fitz-before-sqlite3 ICU landmine), and they
# cover every import the orchestrator actually performs. A dependency missing
# here does not crash loudly: it degrades silently (a missing PaperBanana dep
# drops every paper to the simpler figure pipeline; a broken dashboard.db
# kills status sync for a whole run), which is exactly why they are probed.
PROBES = [
    ("sqlite3+fitz order", "import sqlite3, fitz"),
    ("litellm (Gate A)", "import litellm"),
    ("dashboard db layer", "from website.dashboard import db"),
    ("orchestrator entry", "import sqlite3, ark.orchestrator.core"),
    ("pipeline + latex", "import sqlite3, ark.pipeline, ark.latex.compiler"),
    ("citation + research", "import ark.citation, ark.deep_research"),
    ("delivery contract", "import sqlite3, ark.delivery_contract"),
    ("agent engine", "import ark.engines"),
    ("control plane client", "import ark.controlplane"),
    ("provider SDKs", "import openai, anthropic, google.genai"),
    ("figure pipeline (PaperBanana)",
     "import sys, pathlib; "
     "sys.path.insert(0, str(pathlib.Path('submodules/PaperBanana').resolve())); "
     "import aiofiles, json_repair, matplotlib, huggingface_hub"),
]

# Probed but not required: absence is reported, never a failure. `sky` is the
# cloud (SkyPilot) launcher — the dashboard offers a "☁️ Cloud" backend, but
# no env has ever had skypilot installed and no project has used it, so
# demanding it here would just cry wolf every day.
OPTIONAL_PROBES = [
    ("cloud launcher (SkyPilot)", "import sky"),
]


def env_python(env_name: str) -> Path:
    conda_root = Path(os.environ.get("ARK_CONDA_ROOT", "/data/fat/ark/conda"))
    return conda_root / "envs" / env_name / "bin" / "python"


def baseline_path(env_name: str) -> Path:
    conda_root = Path(os.environ.get("ARK_CONDA_ROOT", "/data/fat/ark/conda"))
    return conda_root / "envs" / f"{env_name}.baseline.json"


def read_versions(py: Path) -> dict:
    """Ask the target env for the versions of the critical packages."""
    code = (
        "import json, importlib.metadata as md\n"
        f"pkgs = {CRITICAL_PACKAGES!r}\n"
        "out = {}\n"
        "for p in pkgs:\n"
        "    try: out[p] = md.version(p)\n"
        "    except md.PackageNotFoundError: out[p] = None\n"
        "print(json.dumps(out))\n"
    )
    r = subprocess.run([str(py), "-c", code], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"version probe failed: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout.strip())


def run_probes(py: Path) -> list[str]:
    """Run functional import probes in the target env; return failure strings."""
    failures = []
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    for name, code in PROBES:
        r = subprocess.run([str(py), "-c", code], capture_output=True,
                           text=True, timeout=120, env=env, cwd="/")
        if r.returncode != 0:
            tail = (r.stderr.strip().splitlines() or ["?"])[-1][:160]
            failures.append(f"probe '{name}' FAILED: {tail}")
    return failures


def diff_state(baseline: dict, current: dict) -> list[str]:
    """Pure comparison: list human-readable drift lines (empty = clean)."""
    drift = []
    for pkg, want in baseline.items():
        have = current.get(pkg)
        if have != want:
            drift.append(f"{pkg}: {want or 'absent'} -> {have or 'absent'}")
    for pkg in current:
        if pkg not in baseline:
            drift.append(f"{pkg}: (not in baseline) -> {current[pkg]}")
    return drift


def notify_admin(env_name: str, problems: list[str]) -> None:
    """Email the admin via the neutral ops-notice pipe. Fail-silent."""
    try:
        sys.path.insert(0, str(REPO))
        from website.dashboard.config import get_settings
        from website.dashboard.notify import send_admin_notice
        s = get_settings()
        admins = getattr(s, "admin_emails", []) or []
        if not admins:
            return
        body = (
            f"Shared env integrity check FAILED for '{env_name}'.\n\n"
            "Someone (or some agent) changed platform-critical dependencies:\n\n  "
            + "\n  ".join(problems)
            + "\n\nImpact: Gate A / figures / control-plane sync may be silently "
              "broken for ALL runs (see 2026-07-10 vllm and 2026-07-18 otree "
              "incidents).\n\nFix:\n"
              "  ark env unlock && <restore versions> && ark env lock\n"
              f"  python scripts/check_arkbase_integrity.py --bless  # if the new state is intended\n"
        )
        send_admin_notice(s, to_email=admins[0],
                          subject=f"[Idea2Paper] Shared env drift: {env_name}",
                          body=body)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", default="ark-base")
    ap.add_argument("--bless", action="store_true",
                    help="record the CURRENT state as the blessed baseline")
    ap.add_argument("--notify", action="store_true",
                    help="email the admin when drift is found (for the timer)")
    args = ap.parse_args()

    py = env_python(args.env)
    if not py.exists():
        print(f"[integrity] env python not found: {py}", file=sys.stderr)
        return 2
    bp = baseline_path(args.env)

    current = read_versions(py)

    if args.bless:
        bp.write_text(json.dumps(current, indent=2) + "\n")
        print(f"[integrity] blessed baseline → {bp}")
        for k, v in current.items():
            print(f"  {k} = {v}")
        return 0

    if not bp.exists():
        print(f"[integrity] no baseline at {bp} — run with --bless first", file=sys.stderr)
        return 2

    baseline = json.loads(bp.read_text())
    problems = diff_state(baseline, current)
    problems += run_probes(py)

    for name, code in OPTIONAL_PROBES:
        r = subprocess.run([str(py), "-c", code], capture_output=True,
                           text=True, timeout=120, env={**os.environ, "PYTHONPATH": str(REPO)})
        if r.returncode != 0:
            print(f"[integrity] note: optional '{name}' unavailable "
                  f"(feature is offered in the UI but cannot run)")

    if not problems:
        print(f"[integrity] {args.env} OK — {len(baseline)} pinned packages match, all probes pass")
        return 0

    print(f"[integrity] {args.env} DRIFT DETECTED:")
    for p in problems:
        print(f"  - {p}")
    if args.notify:
        notify_admin(args.env, problems)
    return 1


if __name__ == "__main__":
    sys.exit(main())
