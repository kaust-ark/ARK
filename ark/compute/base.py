import os
from abc import ABC, abstractmethod
from pathlib import Path

class ComputeBackend(ABC):
    """Base class for all compute backends."""

    def __init__(self, config: dict, project_name: str, code_dir: Path, log_fn=None):
        self.config = config
        self.project_name = project_name
        self.code_dir = Path(code_dir)
        self.log = log_fn or (lambda msg, level="INFO": print(f"[{level}] {msg}"))

    @property
    def _compute_config(self) -> dict:
        return self.config.get("compute_backend", {})

    @abstractmethod
    def setup(self) -> dict:
        """Pre-experiment provisioning. Returns context dict."""
        pass

    @abstractmethod
    def get_agent_instructions(self) -> str:
        """Return instructions to inject into experimenter agent prompt."""
        pass

    @abstractmethod
    def wait_for_completion(self, max_wait_hours: float = 4) -> bool:
        """Block until experiments complete. Returns True if completed."""
        pass

    def collect_results(self) -> bool:
        """Collect results from remote to local. Default: no-op."""
        return True

    def teardown(self):
        """Post-experiment cleanup. Default: no-op."""
        pass
