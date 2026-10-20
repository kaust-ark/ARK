import subprocess
import time
from .base import ComputeBackend

class LocalBackend(ComputeBackend):
    """Run experiments directly on the local machine."""

    @property
    def conda_env(self) -> str:
        explicit = (self._compute_config.get("conda_env")
                    or self.config.get("conda_env"))
        if explicit:
            return explicit
        # Per-project env created by the webapp at <code_dir>/.env.
        local_env = self.code_dir / ".env"
        if (local_env / "conda-meta").is_dir():
            return str(local_env)
        return self.project_name

    @property
    def gpu_count(self) -> int:
        return self._compute_config.get("gpu_count", 0)

    def setup(self) -> dict:
        ctx = {}
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                ctx["gpu_info"] = result.stdout.strip()
                ctx["gpu_count"] = len(result.stdout.strip().split("\n"))
        except Exception:
            ctx["gpu_count"] = 0
        return ctx

    def get_agent_instructions(self) -> str:
        # Under the structural sandbox the agent's shell is INSIDE the
        # container, and the host-machine text below turns into a trap there.
        # It says `conda activate` (no conda exists in the container, and the
        # sandbox directive tells the agent to REPORT unreachable host tools
        # rather than work around them), suggests `nohup` for long tasks, and
        # the generic shell rules add "to wait for results: just exit — the
        # system handles waiting". Three individually-sensible passages that a
        # literally-obedient model reads as one instruction: report, background,
        # exit. A local 32B did exactly that four runs in a row — one turn,
        # zero tool calls, empty results/ — then completed the identical work
        # when told plainly what interpreter to run. The wording is the
        # difference between an agent that experiments and one that resigns.
        try:
            from ark.sandbox import structural_sandbox_status
            in_container = structural_sandbox_status()[0]
        except Exception:
            in_container = False
        if in_container:
            return """## Compute Environment: Inside the Project Container

Your shell already runs inside the project's container; the project directory
is mounted at its usual path and `results/` is shared with the host.

- Run every experiment script in the FOREGROUND with the project interpreter:
      .conda_env/bin/python scripts/exp1.py
  (if `.conda_env` is absent, use `python3`)
- There is NO `conda` command here and none is needed — never run
  `conda activate`, and do not treat its absence as a broken environment.
- Install extra packages with `.conda_env/bin/pip install <pkg>`.
- Do NOT use `nohup` or background processes, and do NOT exit expecting the
  system to wait for anything: nothing runs after you stop. Before finishing,
  verify your result files actually exist in `results/` (e.g. `ls results/`).
- Do NOT use sbatch/srun — Slurm is not reachable from in here."""

        gpu_section = ""
        if self.gpu_count > 0:
            gpu_section = f"\n- Available GPUs: {self.gpu_count}"

        return f"""## Compute Environment: Local Machine

Run experiments directly on this machine. Key settings:
- Conda environment: `{self.conda_env}`
- Activate before running: `conda activate {self.conda_env}`{gpu_section}

Run scripts directly (e.g., `python train.py`). Do NOT use sbatch/srun.
Use `nohup` or background processes for long-running tasks.
Save all results to the `results/` directory."""

    def wait_for_completion(self, max_wait_hours: float = 4) -> bool:
        """Check results/ dir for recent files."""
        scripts_dir = self.config.get("scripts_dir", "scripts")
        possible_dirs = [
            self.code_dir / "results",
            self.code_dir / scripts_dir / "results",
        ]

        results_dir = None
        for d in possible_dirs:
            if d.exists():
                results_dir = d
                break

        if not results_dir:
            self.log("Results directory not found, assuming done.", "WARN")
            return True

        current_time = time.time()
        recent_files = []
        try:
            for file in results_dir.rglob("*"):
                if file.is_file():
                    if current_time - file.stat().st_mtime < max_wait_hours * 3600:
                        recent_files.append(file.name)
        except Exception as e:
            self.log(f"Error checking results: {e}", "WARN")
            return True

        if recent_files:
            self.log(f"Found {len(recent_files)} recent result files.")
            for f in sorted(recent_files)[:5]:
                self.log(f"  - {f}")
            return True
        else:
            self.log("No recent experiment results found.", "WARN")
            return True

    def sync_to_backend(self, source_dir: str, remote_dir: str) -> bool:
        """Push local project files to the compute backend."""
        import shutil
        from pathlib import Path
        src = Path(source_dir).resolve()
        dst = Path(remote_dir).resolve()
        if src == dst:
            return True
        try:
            shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc', 'auto_research'))
            self.log(f"Copied {src} to {dst}")
            return True
        except Exception as e:
            self.log(f"Local sync failed: {e}", "ERROR")
            return False

    def sync_from_backend(self, remote_dir: str, dest_dir: str) -> bool:
        """Pull results from the compute backend back to the orchestrator."""
        import shutil
        from pathlib import Path
        src = Path(remote_dir).resolve()
        dst = Path(dest_dir).resolve()
        if src == dst:
            return True
        try:
            shutil.copytree(src, dst, dirs_exist_ok=True)
            self.log(f"Copied from {src} to {dst}")
            return True
        except Exception as e:
            self.log(f"Local reverse sync failed: {e}", "ERROR")
            return False
