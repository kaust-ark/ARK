import time
from .base import ComputeBackend

class CustomBackend(ComputeBackend):
    """Custom/other backend: user-provided instructions injected into prompt."""

    @property
    def conda_env(self) -> str:
        return (self._compute_config.get("conda_env")
                or self.config.get("conda_env")
                or self.project_name)

    def setup(self) -> dict:
        return {}

    def get_agent_instructions(self) -> str:
        instructions = self._compute_config.get("instructions", "")
        return f"""## Compute Environment: Custom Setup

{instructions}

General settings:
- Conda environment: `{self.conda_env}`
- Save all results to the `results/` directory"""

    def wait_for_completion(self, max_wait_hours: float = 4) -> bool:
        """Check for result files (same as local)."""
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
            self.log("No results dir found, assuming done.", "WARN")
            return True

        current_time = time.time()
        for file in results_dir.rglob("*"):
            if file.is_file():
                if current_time - file.stat().st_mtime < max_wait_hours * 3600:
                    self.log("Found recent results")
                    return True
        return True
