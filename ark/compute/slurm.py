import os
import subprocess
import time
from datetime import datetime, timedelta
from .base import ComputeBackend

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

        return f"""## Compute Environment: Slurm HPC

Use Slurm to submit GPU jobs. Key settings:
- Job name prefix: `{self.job_prefix}`
- Conda environment: `{self.conda_env}`
- Activate before running: `conda activate {self.conda_env}`

Submit jobs using `sbatch`. Name all jobs with prefix `{self.job_prefix}` so the
system can track them (e.g., `#SBATCH --job-name={self.job_prefix}experiment_1`).

Save all results to the `results/` directory.{template_section}"""

    def wait_for_completion(self, max_wait_hours: float = 4) -> bool:
        """Poll squeue for jobs with matching prefix."""
        max_wait = timedelta(hours=max_wait_hours)
        start_time = datetime.now()

        while datetime.now() - start_time < max_wait:
            try:
                result = subprocess.run(
                    ["squeue", "-u", os.environ.get("USER"), "-h", "-o", "%j"],
                    capture_output=True, text=True, timeout=30,
                )
                if not result.stdout.strip():
                    self.log("No Slurm jobs found")
                    return True

                all_jobs = result.stdout.strip().split("\n")
                target_jobs = [j for j in all_jobs if j.startswith(self.job_prefix)]

                if len(target_jobs) == 0:
                    self.log(f"All {self.job_prefix}* jobs completed "
                             f"(other jobs: {len(all_jobs)})")
                    return True

                self.log(f"{len(target_jobs)} {self.job_prefix}* jobs running, "
                         f"waiting 60s...")
                time.sleep(60)

            except Exception as e:
                self.log(f"Error checking Slurm: {e}")
                time.sleep(60)

        self.log(f"Slurm wait timeout after {max_wait_hours} hours")
        return False
