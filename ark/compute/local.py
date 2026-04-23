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
