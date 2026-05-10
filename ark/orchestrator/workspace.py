from __future__ import annotations
import os
import importlib.util
from pathlib import Path
from typing import Optional, Any
import yaml

class WorkspaceManager:
    """Manages project directories, paths, config loading, and symlinks."""
    
    def __init__(
        self, 
        project_name: str, 
        ark_root: Path, 
        project_dir: Optional[str] = None, 
        code_dir: Optional[str] = None,
        logger: Optional[Any] = None
    ):
        self.project_name = project_name
        self.ark_root = ark_root
        self.log = logger or (lambda msg, level="INFO": print(f"[{level}] {msg}"))

        # 1. Resolve Project Path
        if project_dir:
            self.project_path = Path(project_dir).absolute()
        else:
            self.project_path = self.ark_root / "projects" / self.project_name
        
        if not self.project_path.exists():
            raise FileNotFoundError(f"Project directory not found: {self.project_path}")

        # 2. Load Config
        self.config = self._load_config()

        # 3. Resolve Code Directory
        if code_dir:
            self.code_dir = Path(code_dir).absolute()
        else:
            # Default to one level above ARK root if not in config
            default_code_dir = str(self.ark_root.parent)
            self.code_dir = Path(self.config.get("code_dir", default_code_dir)).absolute()

        # 4. Initialize Core Paths
        self.state_dir = self.code_dir / "auto_research" / "state"
        self.log_dir = self.code_dir / "auto_research" / "logs"
        self.agents_dir = self.project_path / "agents"
        
        # Config-driven subpaths
        self.latex_dir = self.code_dir / self.config.get("latex_dir", "paper")
        self.figures_dir = self.code_dir / self.config.get("figures_dir", "paper/figures")

        # 5. Ensure Directories Exist
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.figures_dir.mkdir(parents=True, exist_ok=True)

    def _load_config(self) -> dict:
        config_file = self.project_path / "config.yaml"
        if config_file.exists():
            try:
                with open(config_file) as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                self.log(f"Failed to load config.yaml: {e}", "ERROR")
        return {}

    def setup_workspace(self):
        """Perform workspace setup: chdir, symlinks, and hooks."""
        os.chdir(self.code_dir)
        self.ensure_symlinks()
        return self.load_hooks()

    def ensure_symlinks(self):
        """Ensure project symlinks are present in the code directory."""
        from ark.cli import ensure_project_symlinks
        if self.project_path.resolve() != self.code_dir.resolve():
            ensure_project_symlinks(self.project_path, str(self.code_dir))

    def load_hooks(self):
        """Load project-specific hooks.py if it exists."""
        hooks_file = self.project_path / "hooks.py"
        if hooks_file.exists():
            try:
                spec = importlib.util.spec_from_file_location("hooks", hooks_file)
                hooks = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(hooks)
                return hooks
            except Exception as e:
                self.log(f"Failed to load hooks.py: {e}", "WARN")
        return None
