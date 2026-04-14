"""SLURM job submission, polling, and cancellation; local subprocess fallback."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from jinja2 import Template

_SLURM_TEMPLATE = Path(__file__).parent / "slurm_template.sh"

# Per-project conda env: lives at <project_dir>/.env as a `--prefix` env cloned
# from the configured base env. Detected via the conda-meta directory which
# every conda env has, even an empty one.
PROJECT_ENV_DIRNAME = ".env"


def project_env_prefix(project_dir: Path) -> Path:
    return Path(project_dir) / PROJECT_ENV_DIRNAME


def project_env_ready(project_dir: Path) -> bool:
    return (project_env_prefix(project_dir) / "conda-meta").is_dir()


def find_claude_binary() -> str | None:
    """
    Locate the ``claude`` CLI even when systemd's bare PATH doesn't include
    the user's nvm/npm bin dir. Tries shutil.which, $HOME/.local/bin, then
    every nvm node version's bin dir, then a few common npm prefixes.
    """
    found = shutil.which("claude")
    if found:
        return found
    home = Path(os.path.expanduser("~"))
    candidates: list[Path] = [home / ".local" / "bin" / "claude"]
    nvm_dir = home / ".nvm" / "versions" / "node"
    if nvm_dir.is_dir():
        # Newest version first so we pick the actively used one.
        try:
            for v in sorted(nvm_dir.iterdir(), reverse=True):
                candidates.append(v / "bin" / "claude")
        except OSError:
            pass
    candidates += [
        home / ".npm-global" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def build_subprocess_path(extra: list[str] | None = None) -> str:
    """
    Build a PATH string suitable for spawning ARK subprocesses (orchestrator,
    claude CLI, etc.) when the parent process has a bare systemd PATH.
    Prepends: claude binary dir, ~/.local/bin, texlive 2025 bin, plus any
    caller-supplied dirs, then the existing PATH.
    """
    parts: list[str] = list(extra or [])
    home = Path(os.path.expanduser("~"))

    claude = find_claude_binary()
    if claude:
        parts.append(str(Path(claude).parent))

    parts.append(str(home / ".local" / "bin"))

    texlive = home / "texlive" / "2025" / "bin" / "x86_64-linux"
    if texlive.is_dir():
        parts.append(str(texlive))

    existing = os.environ.get("PATH", "/usr/bin:/bin")
    # Dedupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for p in parts + existing.split(":"):
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return ":".join(out)


def find_conda_binary() -> str | None:
    """
    Locate the conda binary even when PATH is bare (e.g. systemd unit with no
    Environment=PATH=). Tries shutil.which, then $CONDA_EXE, then common
    install prefixes under $HOME.
    """
    found = shutil.which("conda")
    if found:
        return found
    env_var = os.environ.get("CONDA_EXE")
    if env_var and Path(env_var).is_file():
        return env_var
    home = Path(os.path.expanduser("~"))
    for candidate in (
        home / "miniforge3" / "condabin" / "conda",
        home / "miniforge3" / "bin" / "conda",
        home / "miniconda3" / "condabin" / "conda",
        home / "miniconda3" / "bin" / "conda",
        home / "anaconda3" / "condabin" / "conda",
        home / "anaconda3" / "bin" / "conda",
        Path("/opt/conda/bin/conda"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def provision_project_env(project_dir: Path, base_env: str = "ark-base",
                          log_path: Path | None = None) -> tuple[bool, str]:
    """
    Create a per-project conda env at <project_dir>/.env by cloning ``base_env``.

    Returns ``(success, message)``. Idempotent: returns success immediately if
    the env is already present. Writes the conda command output to ``log_path``
    (or <project_dir>/.env_provision.log) for debugging.
    """
    project_dir = Path(project_dir)
    target = project_env_prefix(project_dir)
    log_path = Path(log_path) if log_path else (project_dir / ".env_provision.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if project_env_ready(project_dir):
        return True, f"already exists at {target}"

    conda_bin = find_conda_binary()
    if not conda_bin:
        msg = ("conda binary not found (checked PATH, $CONDA_EXE, and common "
               "miniforge/anaconda locations); cannot provision project env")
        log_path.write_text(msg + "\n")
        return False, msg

    # Stale partial env from a prior failed clone — wipe before retrying.
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)

    cmd = [conda_bin, "create", "--prefix", str(target),
           "--clone", base_env, "--yes"]
    started = time.time()
    try:
        with open(log_path, "w") as lf:
            lf.write(f"$ {' '.join(cmd)}\n")
            lf.flush()
            proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT)
        elapsed = time.time() - started
        if proc.returncode == 0 and project_env_ready(project_dir):
            return True, f"cloned {base_env} in {elapsed:.1f}s"
        return False, f"conda create failed (rc={proc.returncode}); see {log_path}"
    except Exception as e:
        return False, f"conda create raised {type(e).__name__}: {e}"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def slurm_available() -> bool:
    # Escape hatch: set ARK_FORCE_LOCAL=1 to bypass SLURM submission and run
    # everything as local subprocesses (useful when slurmctld spool is full or
    # the cluster is unavailable). Falsy values ("", "0", "false") are ignored.
    force_local = os.environ.get("ARK_FORCE_LOCAL", "").strip().lower()
    if force_local and force_local not in ("0", "false", "no", "off"):
        return False
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
        "lastOnboardingVersion": get_claude_version()
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

    safe_api_keys = {k: shlex.quote(v) for k, v in (api_keys or {}).items()}

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
        api_keys=safe_api_keys,
    )

    if api_keys:
        provision_claude_session(project_dir, api_keys)

    script_path = log_dir / "submit.sh"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        # Securely write script with 0600 permissions immediately
        if script_path.exists():
            script_path.unlink()
        script_path.write_text(script)
        script_path.chmod(0o600)

        result = _run(["sbatch", str(script_path)])
        if result.returncode != 0:
            raise RuntimeError(f"sbatch failed: {result.stderr.strip()}")

        # "Submitted batch job 12345"
        m = re.search(r"(\d+)", result.stdout)
        if not m:
            raise RuntimeError(f"Could not parse job ID from sbatch output: {result.stdout!r}")
        return m.group(1)
    finally:
        # Wipe the script from disk immediately after handing off to SLURM
        if script_path.exists():
            script_path.unlink()


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

    # Build the orchestrator command, preferring the project-local conda env
    # at <project_dir>/.env. Falls back to the named env from settings, then
    # the webapp's own interpreter.
    conda_bin = find_conda_binary()
    local_env = project_env_prefix(project_dir)
    fallback_env = getattr(settings, "slurm_conda_env", "") or ""
    if conda_bin and project_env_ready(project_dir):
        python_prefix = [conda_bin, "run", "--no-capture-output",
                         "--prefix", str(local_env), "python"]
    elif conda_bin and fallback_env:
        python_prefix = [conda_bin, "run", "--no-capture-output",
                         "-n", fallback_env, "python"]
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
    
    # Ensure the orchestrator can find the ark package even when running
    # inside a project-local conda env that doesn't have ark installed.
    ark_code_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = ark_code_root + ((":" + env["PYTHONPATH"]) if env.get("PYTHONPATH") else "")
    env["HOME"] = str(project_dir)
    env["XDG_CONFIG_HOME"] = str(project_dir / ".config")
    # Disable Python's pip user-site discovery so projects are completely
    # isolated — no project can read packages from /home/xinj/.local/... or
    # any other user's user-site. The cloned per-project conda env is the
    # ONLY source of Python packages for the orchestrator.
    env["PYTHONNOUSERSITE"] = "1"
    # Make sure the orchestrator's PATH can find the claude CLI (lives in
    # ~/.nvm/.../bin), latexmk (~/.local/bin), and pdflatex (~/texlive/2025/...).
    # systemd's bare PATH doesn't include any of these.
    env["PATH"] = build_subprocess_path()

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
    """
    Return RUNNING, COMPLETED, or FAILED for a local subprocess job.

    Also reaps the wrapper subprocess if it's a finished child (zombie).
    Plain ``os.kill(pid, 0)`` returns success for zombies — the kernel
    only checks "is this PID slot occupied" — so without this we'd see
    finished projects stuck in RUNNING forever.
    """
    # Try waitpid first; this both checks state AND reaps if it's our
    # child and has finished.
    try:
        wpid, _status = os.waitpid(pid, os.WNOHANG)
        if wpid == 0:
            # Child still running.
            return "RUNNING"
        # wpid == pid → was a zombie, just reaped → fall through to sentinel.
    except ChildProcessError:
        # Not our child (e.g. reparented to PID 1 after a webapp restart,
        # or never our child at all). Fall back to existence check.
        try:
            os.kill(pid, 0)
            return "RUNNING"
        except ProcessLookupError:
            pass
        except PermissionError:
            # Process exists but we can't signal it — it's alive.
            return "RUNNING"

    # Process really has exited — check the sentinel the wrapper writes.
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
