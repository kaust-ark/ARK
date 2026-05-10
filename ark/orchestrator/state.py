from __future__ import annotations
import yaml
import json
import re
from pathlib import Path
from typing import Optional, Any, Tuple

class StateManager:
    """Handles persistence of research state, paper state, and checkpoints with robust error recovery."""

    def __init__(self, state_dir: Path, logger: Optional[Any] = None):
        self.state_dir = state_dir
        self.log = logger or (lambda msg, level="INFO": print(f"[{level}] {msg}"))

        # State file paths
        self.state_file = self.state_dir / "research_state.yaml"
        self.paper_state_file = self.state_dir / "paper_state.yaml"
        self.paper_requirements_file = self.state_dir / "paper_requirements.yaml"
        self.checkpoint_file = self.state_dir / "checkpoint.yaml"
        self.action_plan_file = self.state_dir / "action_plan.yaml"
        self.literature_file = self.state_dir / "literature.yaml"
        self.findings_file = self.state_dir / "findings.yaml"

    # --- Research State ---
    def load_state(self) -> dict:
        if not self.state_file.exists():
            return self._initialize_default_state()
        try:
            with open(self.state_file) as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            self.log(f"Failed to load research_state.yaml: {e}", "ERROR")
            return {}

    def _initialize_default_state(self) -> dict:
        self.log("Initializing new research state...", "INFO")
        default_state = {
            "phases": {
                "C1_quantify": {"status": "pending"},
                "C2_probe": {"status": "pending", "sub_tasks": []},
                "C3_formulate": {"status": "pending"},
                "C4_solver": {"status": "pending"},
                "C5_validation": {"status": "pending"},
            },
            "current_iteration": {"number": 0},
            "history": []
        }
        self.save_state(default_state)
        return default_state

    def save_state(self, state: dict):
        try:
            with open(self.state_file, "w") as f:
                yaml.dump(state, f, default_flow_style=False, allow_unicode=True)
        except Exception as e:
            self.log(f"Failed to save research_state.yaml: {e}", "ERROR")

    # --- Paper State ---
    def load_paper_state(self) -> dict:
        if self.paper_state_file.exists():
            try:
                with open(self.paper_state_file) as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                self.log(f"Failed to load paper_state.yaml: {e}", "ERROR")
        return {
            "reviews": [],
            "current_score": 0,
            "status": "in_progress",
        }

    def save_paper_state(self, state: dict):
        try:
            with open(self.paper_state_file, "w") as f:
                yaml.dump(state, f, default_flow_style=False, allow_unicode=True)
        except Exception as e:
            self.log(f"Failed to save paper_state.yaml: {e}", "ERROR")

    def load_paper_requirements(self) -> dict:
        if self.paper_requirements_file.exists():
            try:
                with open(self.paper_requirements_file) as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                self.log(f"Failed to load paper_requirements.yaml: {e}", "ERROR")
        return {}

    # --- Checkpoints ---
    def save_checkpoint(self, checkpoint_data: dict):
        try:
            with open(self.checkpoint_file, "w") as f:
                yaml.dump(checkpoint_data, f, default_flow_style=False)
        except Exception as e:
            self.log(f"Failed to save checkpoint.yaml: {e}", "ERROR")

    def load_checkpoint(self) -> dict:
        if self.checkpoint_file.exists():
            try:
                with open(self.checkpoint_file) as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                self.log(f"Failed to load checkpoint.yaml: {e}", "ERROR")
        return {}

    def delete_checkpoint(self):
        self.checkpoint_file.unlink(missing_ok=True)

    # --- Action Plan ---
    def load_action_plan(self) -> dict:
        """Load Planner-generated action plan with error recovery for LaTeX escapes."""
        if not self.action_plan_file.exists():
            return {"issues": []}
            
        try:
            with open(self.action_plan_file) as f:
                return yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            self.log(f"YAML parse error in action_plan, attempting to fix LaTeX escape: {e}", "WARN")
            return self._attempt_yaml_fix(self.action_plan_file)
        except Exception as e:
            self.log(f"Failed to load action_plan.yaml: {e}", "ERROR")
            return {"issues": []}

    def _attempt_yaml_fix(self, file_path: Path) -> dict:
        try:
            raw = file_path.read_text()
            def fix_dquoted(match):
                content = match.group(1)
                if '\\' in content:
                    content = content.replace("'", "''")
                    return "'" + content + "'"
                return match.group(0)

            fixed = re.sub(r'"([^"\n]*)"', fix_dquoted, raw)
            file_path.write_text(fixed)
            result = yaml.safe_load(fixed) or {}
            self.log("YAML fix succeeded (LaTeX escape -> single quotes)", "INFO")
            return result
        except Exception as e:
            self.log(f"YAML fix failed: {e}", "ERROR")
            return {"issues": []}

    def save_action_plan(self, action_plan: dict):
        try:
            with open(self.action_plan_file, "w") as f:
                yaml.dump(action_plan, f, default_flow_style=False, allow_unicode=True)
        except Exception as e:
            self.log(f"Failed to save action_plan.yaml: {e}", "ERROR")

    # --- Findings ---
    def load_findings_summary(self) -> str:
        """Load findings.yaml summary, tolerating malformed YAML.

        On parse error, attempts a best-effort auto-repair (the dominant
        agent mistake — a sibling top-level key left indented inside the
        `findings:` list scope). If repair succeeds, the file is
        rewritten in place so the planner gets evidence-aware context on
        this run instead of having to wait for the agent to notice.
        """
        if not self.findings_file.exists():
            return "No findings yet"

        try:
            with open(self.findings_file) as f:
                text = f.read()
            findings = yaml.safe_load(text) or {}
            return yaml.dump(findings, allow_unicode=True)[:500]
        except yaml.YAMLError as e:
            try:
                from ark.findings_schema import attempt_repair
                repaired, changes = attempt_repair(text)
            except Exception:
                repaired, changes = None, []
            if repaired is not None:
                try:
                    backup = self.findings_file.with_suffix(".yaml.malformed")
                    backup.write_text(text)
                    self.findings_file.write_text(repaired)
                    findings = yaml.safe_load(repaired) or {}
                    detail = "; ".join(changes) if changes else "auto-repaired"
                    self.log(
                        f"findings.yaml had a parse error and was auto-repaired "
                        f"({detail}); original saved to {backup.name}",
                        "INFO",
                    )
                    return yaml.dump(findings, allow_unicode=True)[:500]
                except Exception as repair_err:
                    self.log(
                        f"findings.yaml auto-repair failed at write step: {repair_err}",
                        "WARN",
                    )
            detail = "; ".join(changes) if changes else "no known repair pattern matched"
            self.log(
                f"findings.yaml is malformed ({type(e).__name__}); planner will "
                f"proceed without it. Repair attempt: {detail}.",
                "WARN",
            )
            return f"[findings.yaml unparseable: {e.__class__.__name__}]"
        except Exception:
            return "No findings yet"
