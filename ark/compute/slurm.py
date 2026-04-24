import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from .base import ComputeBackend


# Liveness check parameters for wait_for_completion.
# When the soft deadline (max_wait_hours) is reached, we consult these to
# decide between "still progressing, keep waiting" and "truly stuck, fail".
_STDOUT_LIVENESS_WINDOW = timedelta(minutes=5)
# Hard ceiling multiplier over max_wait_hours — even an alive job cannot
# extend waiting past this, to bound total compute spend.
_HARD_CEILING_MULTIPLIER = 3


def _poll_interval_seconds(elapsed: timedelta) -> int:
    """Grow polling interval to keep logs readable on long waits."""
    m = elapsed.total_seconds() / 60
    if m < 15:
        return 60
    if m < 60:
        return 120
    return 300


class SlurmBackend(ComputeBackend):
    """MCNodes / Slurm HPC backend."""

    @property
    def job_prefix(self) -> str:
        return (self._compute_config.get("job_prefix")
                or self.config.get("slurm_job_prefix")
                or f"{self.project_name.upper()}_")

    @property
    def conda_env(self) -> str:
        explicit = (self._compute_config.get("conda_env")
                    or self.config.get("conda_env"))
        if explicit:
            return explicit
        local_env = self.code_dir / ".env"
        if (local_env / "conda-meta").is_dir():
            return str(local_env)
        return self.project_name

    @property
    def slurm_template(self) -> str:
        return self._compute_config.get("slurm_template", "")

    def setup(self) -> dict:
        """Auto-discover cluster info via sinfo/sacctmgr."""
        ctx = {}
        try:
            result = subprocess.run(
                ["sinfo", "-o", "%P %G %c %m %a"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                ctx["cluster_info"] = result.stdout.strip()
                self.log(f"Cluster discovered: {len(result.stdout.strip().splitlines())} partitions")
        except Exception as e:
            self.log(f"sinfo discovery failed: {e}", "WARN")

        try:
            result = subprocess.run(
                ["sacctmgr", "show", "assoc",
                 f"user={os.environ.get('USER', '')}",
                 "format=Account", "-n"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                ctx["slurm_account"] = result.stdout.strip().split("\n")[0].strip()
        except Exception:
            pass

        return ctx

    def _is_rocs_testbed(self) -> bool:
        """Detect the SANDS Lab / KAUST ROCS testbed by its node naming.

        We look for nodes matching `mcnode*` in `sinfo -h -N -o %N`. This
        is cheap (one subprocess call) and specific enough to avoid false
        positives on IBEX or other KAUST clusters.
        """
        try:
            r = subprocess.run(
                ["sinfo", "-h", "-N", "-o", "%N"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return False
            nodes = r.stdout.split()
            return any(n.startswith("mcnode") for n in nodes)
        except Exception:
            return False

    def get_agent_instructions(self) -> str:
        template_section = ""
        if self.slurm_template:
            path = self.code_dir / self.slurm_template
            if path.exists():
                template_section = (
                    f"\n\n## Slurm Template\n\n"
                    f"Use this as a base for your .slurm scripts:\n"
                    f"```\n{path.read_text()}\n```"
                )

        # Cluster-specific guidance. The ROCS checklist is inlined (not just
        # referenced) because a full SKILL.md Read is an extra tool call the
        # experimenter may skip; we want the *most-forgotten* rules — the
        # --login shebang and the GPU --gres line — to land in the prompt
        # unconditionally. The skill file carries deep reference material.
        cluster_section = ""
        if self._is_rocs_testbed():
            # Absolute path to the master skill copy — always readable by
            # the experimenter regardless of whether the researcher
            # selected it via selected_skills.json.
            skill_path = (
                Path(__file__).parent.parent.parent
                / "skills" / "library" / "hpc"
                / "rocs-testbed-slurm" / "SKILL.md"
            )
            cluster_section = f"""

## Cluster: SANDS Lab ROCS Testbed (KAUST)

This cluster has GPUs on the `mc` partition, but **SLURM only gives you a
GPU if you explicitly request one via `--gres`**. A sbatch script that
omits `--gres` runs on a GPU-equipped node with `torch.cuda.is_available()
== False` — silent CPU fallback, 20–100× slowdown, often triggering the
pipeline's wait-timeout. The experimenter run on 2026-04-23 lost an entire
iteration to exactly this mistake.

**Non-negotiable rules for every sbatch script:**

1. Activate conda by sourcing `$(conda info --base)/etc/profile.d/conda.sh`
   *before* `conda activate`. `#!/bin/bash --login` alone is NOT enough:
   it only sources `.bashrc` / `.bash_profile`, and those files only
   register the `conda activate` shell function if the user previously
   ran `conda init bash`. Sourcing the profile.d script works regardless.
2. If the code trains a NN / uses PyTorch / JAX / TensorFlow: include `#SBATCH --gres=gpu:<type>:1`
3. Pick the weakest GPU that works: **V100 > A100** (prefer V100 unless model truly needs more; do not use p100)
4. If NOT using GPU: OMIT `--gres=gpu` entirely (don't burn quota)
5. Always set `--time=...` (never default)
6. Activate the **project-local** env at `{self.conda_env}` — not a global
   env like `ark-base`. The project env has the versions pinned for this
   experiment; switching to a shared env produces unreproducible results.

**Canonical GPU sbatch skeleton:**
```bash
#!/bin/bash
#SBATCH --job-name={self.job_prefix}experiment_1
#SBATCH --partition=mc
#SBATCH --time=04:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --gres=gpu:v100:1
#SBATCH --output=results/<exp_dir>/slurm_%j.out
#SBATCH --error=results/<exp_dir>/slurm_%j.err

set -e
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate "{self.conda_env}"
python scripts/train.py
```

**QoS caps (normal):** A100=2, V100=8 concurrent. For jobs >3 days
use `--qos=spot` (preemptible, checkpoint via SIGUSR1).

**Watchdog:** jobs with <15% GPU utilization for 2 consecutive hours are
auto-cancelled. If the code is CPU-bound, drop the GPU request.

**Full reference** (A100 PCI vs SXM constraints, DeepSpeed multi-node
template, preemption signal handlers, `jobstats`/`ninfo`/`ginfo` usage):
`{skill_path}` — Read this before writing non-trivial sbatch scripts."""

        return f"""## Compute Environment: Slurm HPC

Use Slurm to submit GPU jobs. Key settings:
- Job name prefix: `{self.job_prefix}`
- Conda environment: `{self.conda_env}` (project-local — do not substitute a shared env)
- Activate before running (both lines required):
  ```bash
  source "$(conda info --base)/etc/profile.d/conda.sh"
  conda activate "{self.conda_env}"
  ```
  The `source` line registers the `conda activate` shell function. Without
  it, the bare `conda activate` command fails with
  `CondaError: Run 'conda init' before 'conda activate'` on hosts where
  the user has not run `conda init bash`. The `source` form is portable
  and works regardless of shell-init state.

Submit jobs using `sbatch`. Name all jobs with prefix `{self.job_prefix}` so the
system can track them (e.g., `#SBATCH --job-name={self.job_prefix}experiment_1`).

Save all results to the `results/` directory.{cluster_section}{template_section}"""

    def _query_target_jobs(self) -> tuple[list[tuple[str, str]], int]:
        """Query squeue for jobs owned by this user.

        Returns (target_jobs, other_count). target_jobs is a list of
        (jobid, jobname) tuples whose name starts with self.job_prefix.
        Raises on squeue failure — caller decides how to handle.
        """
        result = subprocess.run(
            ["squeue", "-u", os.environ.get("USER", ""), "-h", "-o", "%i %j"],
            capture_output=True, text=True, timeout=30,
            check=True,
        )
        target, other = [], 0
        for line in result.stdout.strip().splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            jobid, jobname = parts
            if jobname.startswith(self.job_prefix):
                target.append((jobid, jobname))
            else:
                other += 1
        return target, other

    def _job_stdout_path(self, jobid: str) -> str | None:
        """Ask scontrol where a given job is writing its stdout."""
        try:
            r = subprocess.run(
                ["scontrol", "show", "job", str(jobid), "-o"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                return None
            for tok in r.stdout.split():
                if tok.startswith("StdOut="):
                    return tok[len("StdOut="):]
        except Exception:
            pass
        return None

    def _liveness_report(self, jobs: list[tuple[str, str]],
                         window: timedelta) -> tuple[bool, list[str]]:
        """Return (any_alive, per-job detail lines) based on stdout mtime.

        Conservative: if no job stdout can be checked at all, returns
        alive=True — absence of evidence is not evidence of death.
        """
        now = datetime.now()
        alive = False
        checked = 0
        details: list[str] = []
        for jobid, jobname in jobs:
            path = self._job_stdout_path(jobid)
            if not path:
                details.append(f"  {jobname}({jobid}): StdOut path unavailable")
                continue
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(path))
            except OSError as e:
                details.append(f"  {jobname}({jobid}): stdout unreadable ({e})")
                continue
            checked += 1
            age = now - mtime
            age_s = int(age.total_seconds())
            tag = "alive" if age < window else "idle"
            if age < window:
                alive = True
            details.append(f"  {jobname}({jobid}): stdout {age_s}s ago [{tag}]")
        if checked == 0:
            alive = True
            details.append("  (no stdout could be inspected; assuming alive)")
        return alive, details

    def wait_for_completion(self, max_wait_hours: float = 4) -> bool:
        """Poll squeue for jobs with matching prefix.

        Two-stage timeout:
          * Soft deadline = start + max_wait_hours. When hit, inspect each
            target job's stdout mtime. If any file was written within
            _STDOUT_LIVENESS_WINDOW, extend the soft deadline by another
            max_wait_hours (capped at the hard ceiling).
          * Hard ceiling = start + max_wait_hours * _HARD_CEILING_MULTIPLIER.
            Never extended. Prevents zombie jobs from consuming the whole
            project budget.

        Poll interval grows from 60s to 300s as elapsed time increases, to
        keep long-wait logs readable.
        """
        start_time = datetime.now()
        soft_deadline = start_time + timedelta(hours=max_wait_hours)
        hard_deadline = start_time + timedelta(
            hours=max_wait_hours * _HARD_CEILING_MULTIPLIER
        )
        extension = timedelta(hours=max_wait_hours)

        while datetime.now() < hard_deadline:
            try:
                target_jobs, other_count = self._query_target_jobs()
            except Exception as e:
                self.log(f"Error checking Slurm: {e}")
                time.sleep(60)
                continue

            if not target_jobs:
                self.log(f"All {self.job_prefix}* jobs completed "
                         f"(other jobs: {other_count})")
                return True

            now = datetime.now()
            elapsed = now - start_time

            if now >= soft_deadline:
                alive, details = self._liveness_report(
                    target_jobs, _STDOUT_LIVENESS_WINDOW
                )
                if alive:
                    soft_deadline = min(now + extension, hard_deadline)
                    remaining_min = int((soft_deadline - now).total_seconds() / 60)
                    self.log(
                        f"Past soft deadline but {len(target_jobs)} "
                        f"{self.job_prefix}* job(s) still writing stdout; "
                        f"extending deadline by {remaining_min}min"
                    )
                    for line in details:
                        self.log(line)
                else:
                    window_min = int(_STDOUT_LIVENESS_WINDOW.total_seconds() / 60)
                    self.log(
                        f"Slurm wait timeout after {elapsed}: "
                        f"{len(target_jobs)} {self.job_prefix}* job(s) in queue "
                        f"but stdout idle >{window_min}min"
                    )
                    for line in details:
                        self.log(line)
                    return False

            interval = _poll_interval_seconds(elapsed)
            # Only log the routine "waiting" line before soft deadline — after
            # that, the liveness report above carries the signal and we don't
            # want to double-log.
            if now < soft_deadline:
                self.log(
                    f"{len(target_jobs)} {self.job_prefix}* jobs running, "
                    f"waiting {interval}s..."
                )
            time.sleep(interval)

        hard_hours = max_wait_hours * _HARD_CEILING_MULTIPLIER
        self.log(f"Slurm wait hard ceiling reached after {hard_hours} hours")
        return False
