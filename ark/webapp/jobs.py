"""SLURM job submission, polling, and cancellation; local subprocess fallback."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from jinja2 import Template

_SLURM_TEMPLATE = Path(__file__).parent / "slurm_template.sh"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def slurm_available() -> bool:
    return shutil.which("sbatch") is not None


_CLAUDE_VERSION_CACHE = None

def get_claude_version() -> str:
    """Detect and cache the current version of the Claude CLI."""
    global _CLAUDE_VERSION_CACHE
    if _CLAUDE_VERSION_CACHE:
        return _CLAUDE_VERSION_CACHE
    try:
        r = _run(["claude", "--version"])
        # Output: "2.0.32 (Claude Code)"
        m = re.search(r"([\d\.]+)", r.stdout)
        if m:
            _CLAUDE_VERSION_CACHE = m.group(1)
            return _CLAUDE_VERSION_CACHE
    except Exception:
        pass
    return "0.1.0"


def provision_claude_session(target_dir: Path, keys: dict[str, str]):
    """
    Writes a manual ~/.claude.json to skip onboarding.
    Requires oauth_token, account_uuid, email, and org_uuid in keys.
    """
    if "claude_oauth_token" not in keys:
        return
    
    config = {
        "hasCompletedOnboarding": True,
        "lastOnboardingVersion": get_claude_version(),
        "oauthAccount": {
            "accountUuid": keys.get("claude_account_uuid", ""),
            "emailAddress": keys.get("claude_email", ""),
            "organizationUuid": keys.get("claude_org_uuid", "")
        }
    }
    
    target_dir.mkdir(parents=True, exist_ok=True)
    import json
    (target_dir / ".claude.json").write_text(json.dumps(config))


def _auto_partition() -> str:
    """Try to detect an available partition from sinfo."""
    try:
        r = _run(["sinfo", "-h", "-o", "%P"])
        partitions = [p.rstrip("*") for p in r.stdout.split() if p]
        # Prefer non-GPU partitions (ARK uses no GPU)
        for p in partitions:
            if "gpu" not in p.lower():
                return p
        return partitions[0] if partitions else ""
    except Exception:
        return ""


def _auto_account() -> str:
    """Try to detect default account from sacctmgr."""
    try:
        r = _run(["sacctmgr", "-n", "show", "user", "-s", "format=defaultaccount"])
        accounts = r.stdout.split()
        return accounts[0] if accounts else ""
    except Exception:
        return ""


def submit_job(
    project_id: str,
    mode: str,
    max_iterations: int,
    project_dir: Path,
    log_dir: Path,
    settings,
    api_keys: dict[str, str] = None,
) -> str:
    """Render slurm_template.sh and submit via sbatch. Returns job_id string."""
    partition = settings.slurm_partition or _auto_partition()
    account = settings.slurm_account or _auto_account()

    template_text = _SLURM_TEMPLATE.read_text()
    script = Template(template_text).render(
        project_id=project_id,
        project_dir=str(project_dir),
        log_dir=str(log_dir),
        mode=mode,
        max_iterations=max_iterations,
        partition=partition,
        account=account,
        gres=settings.slurm_gres,
        cpus_per_task=settings.slurm_cpus_per_task,
        conda_env=settings.slurm_conda_env,
        api_keys=api_keys or {},
    )

    if api_keys:
        provision_claude_session(project_dir, api_keys)

    script_path = log_dir / "submit.sh"
    log_dir.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script)
    script_path.chmod(0o755)

    result = _run(["sbatch", str(script_path)])
    if result.returncode != 0:
        raise RuntimeError(f"sbatch failed: {result.stderr.strip()}")

    # "Submitted batch job 12345"
    m = re.search(r"(\d+)", result.stdout)
    if not m:
        raise RuntimeError(f"Could not parse job ID from sbatch output: {result.stdout!r}")
    return m.group(1)


def poll_job(job_id: str) -> str:
    """Return current job state: PENDING, RUNNING, COMPLETED, FAILED, CANCELLED, UNKNOWN."""
    try:
        r = _run(["squeue", "-j", job_id, "-h", "-o", "%T"])
        state = r.stdout.strip()
        if state:
            return state  # PENDING / RUNNING / ...
        # Job not in squeue → check sacct
        r2 = _run(["sacct", "-j", job_id, "-n", "-o", "State", "--noconvert"])
        lines = [l.strip() for l in r2.stdout.splitlines() if l.strip()]
        return lines[0] if lines else "COMPLETED"
    except Exception:
        return "UNKNOWN"


def cancel_job(job_id: str) -> bool:
    """Cancel a SLURM job. Returns True on success."""
    try:
        r = _run(["scancel", job_id])
        return r.returncode == 0
    except Exception:
        return False


def slurm_state_to_status(slurm_state: str) -> str:
    """Map SLURM state to ARK webapp status."""
    s = slurm_state.upper()
    if s in ("PENDING", "CONFIGURING", "REQUEUED"):
        return "queued"
    if s in ("RUNNING", "COMPLETING"):
        return "running"
    if s in ("COMPLETED",):
        return "done"
    if s in ("CANCELLED", "TIMEOUT", "PREEMPTED"):
        return "stopped"
    # FAILED, NODE_FAIL, OUT_OF_MEMORY, etc.
    return "failed"


# ── Local subprocess runner ────────────────────────────────────────────────────

def launch_local_job(
    project_id: str,
    mode: str,
    max_iterations: int,
    project_dir: Path,
    log_dir: Path,
    settings,
    api_keys: dict[str, str] = None,
) -> str:
    """Launch orchestrator as a local subprocess. Returns 'local:{pid}'."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"local_{int(time.time())}.out"
    exit_file = log_dir / "local_exit.txt"
    exit_file.unlink(missing_ok=True)

    # Build the orchestrator command, preferring the configured conda env.
    conda_env = getattr(settings, "slurm_conda_env", "") or ""
    conda_bin = shutil.which("conda") if conda_env else None
    if conda_bin and conda_env:
        python_prefix = [conda_bin, "run", "--no-capture-output", "-n", conda_env, "python"]
    else:
        python_prefix = [sys.executable]

    cmd = python_prefix + [
        "-m", "ark.orchestrator",
        "--project", project_id,
        "--project-dir", str(project_dir),
        "--code-dir", str(project_dir),
        "--mode", mode,
        "--iterations", str(max_iterations),
    ]

    # Inline wrapper (uses webapp's Python — only needs subprocess + pathlib).
    # Runs orchestrator in the correct env, then writes exit code to sentinel.
    wrapper = (
        "import subprocess as _s\n"
        f"_r = _s.run({cmd!r})\n"
        f"open({str(exit_file)!r}, 'w').write(str(_r.returncode))\n"
    )

    # Prepare environment with user keys and home isolation
    env = os.environ.copy()
    if api_keys:
        provision_claude_session(project_dir, api_keys)
        for k, v in api_keys.items():
            if k == "claude_oauth_token":
                env["CLAUDE_CODE_OAUTH_TOKEN"] = v
            elif k.endswith("_api_key") or k in ("gemini", "anthropic", "openai"):
                # Standard LLM keys
                env_key = f"{k.upper()}_API_KEY" if "_api_key" not in k.lower() else k.upper()
                env[env_key] = v
    
    env["HOME"] = str(project_dir)
    env["XDG_CONFIG_HOME"] = str(project_dir / ".config")

    with open(log_file, "w") as lf:
        proc = subprocess.Popen(
            [sys.executable, "-c", wrapper],
            stdout=lf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(project_dir),
            env=env,
        )

    return f"local:{proc.pid}"


def poll_local_job(pid: int, log_dir: Path) -> str:
    """Return RUNNING, COMPLETED, or FAILED for a local subprocess job."""
    try:
        os.kill(pid, 0)
        return "RUNNING"
    except ProcessLookupError:
        pass
    except PermissionError:
        return "RUNNING"

    # Process has exited — check sentinel
    exit_file = Path(log_dir) / "local_exit.txt"
    if exit_file.exists():
        code = exit_file.read_text().strip()
        return "COMPLETED" if code == "0" else "FAILED"
    return "FAILED"


def cancel_local_job(pid: int) -> bool:
    """Send SIGTERM to the process group of a local job."""
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
        return True
    except Exception:
        return False
