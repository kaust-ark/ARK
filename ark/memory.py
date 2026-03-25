#!/usr/bin/env python3
"""
AutoGAC Memory System - Enhanced Version

Features:
1. Score history tracking
2. Stagnation detection
3. Goal Anchor
4. Issue repeat tracking (new)
5. Repair verification (new)
6. Meta-reflection checks (new)

Design principle: Trust AI's judgment; code only handles execution and safeguards
"""

import yaml
from datetime import datetime
from pathlib import Path
from typing import Tuple, List, Dict, Optional

# Default paths (can be overridden per-instance)
_default_state_dir = None  # Set by get_memory() or Orchestrator

# Configuration
STAGNATION_THRESHOLD = 5
MIN_PROGRESS_DELTA = 0.3

# Issue type classification keywords
TECHNICAL_KEYWORDS = [
    "missing data", "no validation", "insufficient evidence", "benchmark",
    "experiment", "evaluation", "comparison", "e2e", "end-to-end",
    "perplexity", "accuracy", "latency measurement", "no proof"
]
PRESENTATION_KEYWORDS = [
    "font", "size", "layout", "overlap", "caption", "spacing", "margin",
    "related work", "citation", "figure", "table", "color", "width",
    "crowded", "dense", "readability", "visual", "formatting"
]

# Goal Anchor - Prevent deviation from the main direction
GOAL_ANCHOR = """
## Paper Core Objectives (Goal Anchor)

**Paper title**: When Smaller Is Slower: Dimensional Collapse in Compressed LLMs
**Target venue**: EuroMLSys 2026 (SIGPLAN format, 6 pages body text, references and appendix unlimited)

**Core contributions** (must not deviate):
1. Discover and quantify the Dimensional Collapse phenomenon
2. Analyze root causes of GPU performance cliffs (TC, Vec, BW, L2)
3. Propose the GAC dimension repair strategy
4. End-to-end validation of repair effectiveness

**Key constraints**:
- 6-page limit (excluding references)
- Maintain technical depth, avoid being superficial
- Every argument must be supported by data
"""


class SimpleMemory:
    """Enhanced iteration memory - tracks scores, stagnation, issue repetition, repair verification"""

    def __init__(self, state_dir: Path = None):
        self.scores: List[float] = []
        self.best_score: float = 0.0
        self.stagnation_count: int = 0
        self.goal_anchor: str = ""  # Dynamic Goal Anchor

        # Issue repeat tracking
        self.issue_history: Dict[str, int] = {}  # issue_id -> occurrence count
        self.last_issues: List[str] = []  # Issues from last review

        # Issue repair method history (records what methods were used for each issue)
        self.issue_repair_methods: Dict[str, List[str]] = {}  # issue_id -> [method list]

        # Repair verification
        self.expected_changes: Dict[str, str] = {}  # file_path -> change_type
        self.last_repair_iteration: int = 0  # Iteration number of last self_repair
        self.repair_effective: Optional[bool] = None  # Whether last repair was effective

        # Meta-Debugger support
        self.experiment_empty_count: int = 0  # Experiment idle run count

        # Determine memory file path
        if state_dir is not None:
            self.memory_file = Path(state_dir) / "memory.yaml"
        elif _default_state_dir is not None:
            self.memory_file = Path(_default_state_dir) / "memory.yaml"
        else:
            # Legacy fallback: auto_research/state/ relative to cwd
            self.memory_file = Path("auto_research") / "state" / "memory.yaml"

        self.load()

    def load(self):
        """Load from file"""
        if self.memory_file.exists():
            try:
                data = yaml.safe_load(self.memory_file.read_text()) or {}
                self.scores = data.get("scores", [])
                self.best_score = data.get("best_score", 0.0)
                self.stagnation_count = data.get("stagnation_count", 0)
                # Additional fields
                self.issue_history = data.get("issue_history", {})
                self.last_issues = data.get("last_issues", [])
                self.issue_repair_methods = data.get("issue_repair_methods", {})
                self.expected_changes = data.get("expected_changes", {})
                self.last_repair_iteration = data.get("last_repair_iteration", 0)
                self.repair_effective = data.get("repair_effective")
                # Meta-Debugger support
                self.experiment_empty_count = data.get("experiment_empty_count", 0)
            except Exception:
                pass

    def save(self):
        """Save to file"""
        self.memory_file.parent.mkdir(parents=True, exist_ok=True)
        self.memory_file.write_text(yaml.dump({
            "scores": self.scores[-20:],  # Keep only the most recent 20
            "best_score": self.best_score,
            "stagnation_count": self.stagnation_count,
            # Issue tracking
            "issue_history": self.issue_history,
            "last_issues": self.last_issues,
            "issue_repair_methods": self.issue_repair_methods,
            "expected_changes": self.expected_changes,
            "last_repair_iteration": self.last_repair_iteration,
            "repair_effective": self.repair_effective,
            # Meta-Debugger support
            "experiment_empty_count": getattr(self, 'experiment_empty_count', 0),
            "last_updated": datetime.now().isoformat(),
        }, allow_unicode=True))

    def record_score(self, score: float):
        """Record a new score"""
        prev_score = self.scores[-1] if self.scores else 0.0
        self.scores.append(score)

        # Update best score
        if score > self.best_score:
            self.best_score = score

        # Stagnation detection
        if (score - prev_score) >= MIN_PROGRESS_DELTA:
            self.stagnation_count = 0  # Effective progress, reset
        else:
            self.stagnation_count += 1

        self.save()

    def is_stagnating(self) -> Tuple[bool, str]:
        """Detect whether stagnating"""
        if self.stagnation_count >= STAGNATION_THRESHOLD:
            return True, f"{self.stagnation_count} consecutive iterations without effective progress (delta < {MIN_PROGRESS_DELTA})"

        # Check if stuck going in circles
        if len(self.scores) >= 6:
            recent = self.scores[-6:]
            variance = max(recent) - min(recent)
            if variance < 0.5:
                return True, f"Last 6 score fluctuations too small ({variance:.2f})"

        return False, ""

    def get_context(self) -> str:
        """Get simple context (for agents)"""
        lines = []
        if self.goal_anchor:
            lines.append(self.goal_anchor)
            lines.append("")

        # Stagnation warning
        is_stuck, reason = self.is_stagnating()
        if is_stuck:
            lines.append(f"**Stagnation Warning**: {reason}")
            lines.append("Suggestion: Try a completely different approach, or supplement with experimental data")
            lines.append("")

        # Score trend
        if self.scores:
            lines.append("## Score Trend")
            lines.append(f"- Current: **{self.scores[-1]}/10**")
            lines.append(f"- Best: **{self.best_score}/10**")
            lines.append(f"- History: {' -> '.join(f'{s:.1f}' for s in self.scores[-5:])}")
            lines.append("")

        # Add self-check report (repeated Issue warnings, etc.)
        self_check = self.get_self_check_report()
        if self_check:
            lines.append(self_check)

        return "\n".join(lines)

    def set_goal_anchor(self, anchor_text: str):
        """Set the project's Goal Anchor"""
        self.goal_anchor = anchor_text

    def get_context_for_agent(self, agent_type: str) -> str:
        """Legacy interface compatibility"""
        return self.get_context()

    # ==================== Issue Repeat Tracking ====================

    def record_issues(self, issues: List[str], iteration: int):
        """Record issues that appeared in the current review

        Args:
            issues: List of Issue IDs, e.g. ["M1", "M2", "m1"]
            iteration: Current iteration number
        """
        current_set = set(issues)
        last_set = set(self.last_issues)

        # Issues that were resolved (present last time but gone now)
        resolved = last_set - current_set
        for issue_id in resolved:
            # Reset count for resolved issues — they were fixed
            if issue_id in self.issue_history:
                self.issue_history[issue_id] = 0

        # Check if last repair was effective (if applicable)
        if self.last_repair_iteration > 0:
            repeat_issues = current_set & last_set
            if repeat_issues:
                self.repair_effective = False
            else:
                self.repair_effective = True

        # Update issue history: only increment for issues that persist from last review
        # New issues (not in last review) start at 1
        # Returning issues (in both reviews) get incremented
        for issue_id in issues:
            if issue_id in last_set:
                # Persisting issue — increment
                self.issue_history[issue_id] = self.issue_history.get(issue_id, 0) + 1
            else:
                # New issue — start at 1
                self.issue_history[issue_id] = 1

        self.last_issues = issues
        self.save()

    def get_repeat_issues(self, threshold: int = 3) -> List[Tuple[str, int]]:
        """Get repeatedly occurring issues

        Args:
            threshold: Occurrence count threshold, default 3

        Returns:
            List of (issue_id, count) tuples for issues appearing >= threshold times
        """
        return [(k, v) for k, v in self.issue_history.items() if v >= threshold]

    def get_issue_count(self, issue_id: str) -> int:
        """Get the occurrence count of a specific issue"""
        return self.issue_history.get(issue_id, 0)

    # ==================== Repair Method History ====================

    def record_repair_method(self, issue_id: str, method: str):
        """Record the repair method used for an issue (allows duplicates, tracks attempt count)

        Args:
            issue_id: Issue ID, e.g. "M1"
            method: Repair method, e.g. "WRITING_ONLY", "FIGURE_CODE_REQUIRED"
        """
        if issue_id not in self.issue_repair_methods:
            self.issue_repair_methods[issue_id] = []
        # Allow duplicate additions so we can track how many times the same method was attempted
        self.issue_repair_methods[issue_id].append(method)
        self.save()

    def get_tried_methods(self, issue_id: str) -> List[str]:
        """Get the repair methods already tried for a specific issue"""
        return self.issue_repair_methods.get(issue_id, [])

    def classify_issue_type(self, issue_description: str) -> str:
        """Classify issue type based on description

        Returns:
            "technical" - Issues requiring experimental data support
            "presentation" - Layout/visual/writing issues
        """
        desc_lower = issue_description.lower()

        # First check if it's a technical issue
        for keyword in TECHNICAL_KEYWORDS:
            if keyword in desc_lower:
                return "technical"

        # Check if it's a presentation issue
        for keyword in PRESENTATION_KEYWORDS:
            if keyword in desc_lower:
                return "presentation"

        # Default to presentation issue (safer, won't trigger unnecessary experiments)
        return "presentation"

    def get_banned_methods(self, issue_id: str, issue_description: str = "") -> List[str]:
        """Get the list of banned methods for a specific issue

        Logic: differentiate handling based on issue type
        - PRESENTATION issues: never force EXPERIMENT_REQUIRED
        - TECHNICAL issues: can escalate to EXPERIMENT_REQUIRED

        Args:
            issue_id: Issue ID
            issue_description: Issue description (for classification)
        """
        count = self.get_issue_count(issue_id)
        tried = self.get_tried_methods(issue_id)
        issue_type = self.classify_issue_type(issue_description)

        # PRESENTATION issues: cycle through methods, don't force EXPERIMENT
        if issue_type == "presentation":
            # If WRITING_ONLY tried 3+ times but FIGURE_CODE not tried, suggest switching
            writing_tries = tried.count("WRITING_ONLY") if tried else 0
            figure_tries = tried.count("FIGURE_CODE_REQUIRED") if tried else 0

            if writing_tries >= 3 and figure_tries < 2:
                return ["WRITING_ONLY"]  # Ban WRITING_ONLY, suggest FIGURE_CODE
            elif figure_tries >= 3 and writing_tries < 2:
                return ["FIGURE_CODE_REQUIRED"]  # Reverse
            # No bans in other cases
            return []

        # TECHNICAL issues: can escalate to EXPERIMENT
        if count >= 10:
            return ["WRITING_ONLY", "FIGURE_CODE_REQUIRED", "LITERATURE_REQUIRED"]
        elif count >= 5:
            return ["WRITING_ONLY", "FIGURE_CODE_REQUIRED"]
        elif count >= 3:
            return ["WRITING_ONLY"]
        return []

    def get_strategy_escalation(self, issue_descriptions: Dict[str, str] = None) -> Dict[str, dict]:
        """Get issues that need strategy escalation

        Args:
            issue_descriptions: Optional dict of issue_id -> description for better classification

        Returns:
            Dict of issue_id -> {count, tried_methods, banned_methods, required_escalation, issue_type}
        """
        escalations = {}
        issue_descriptions = issue_descriptions or {}

        for issue_id, count in self.issue_history.items():
            if count >= 3:  # 3+ repetitions need attention
                tried = self.get_tried_methods(issue_id)
                desc = issue_descriptions.get(issue_id, "")
                issue_type = self.classify_issue_type(desc)
                banned = self.get_banned_methods(issue_id, desc)

                # Determine escalation direction based on issue type
                required = None

                if issue_type == "presentation":
                    # Presentation issues: cycle through different methods
                    if "WRITING_ONLY" in tried and "FIGURE_CODE_REQUIRED" not in tried:
                        required = "FIGURE_CODE_REQUIRED (modify Python plotting scripts)"
                    elif "FIGURE_CODE_REQUIRED" in tried and "LITERATURE_REQUIRED" not in tried:
                        required = "LITERATURE_REQUIRED (supplement citations and Related Work)"
                    elif all(m in tried for m in ["WRITING_ONLY", "FIGURE_CODE_REQUIRED"]):
                        required = "Try a completely different approach, or check if the issue truly exists"
                else:
                    # Technical issues: can escalate to EXPERIMENT
                    if count >= 7:
                        required = "EXPERIMENT_REQUIRED (need new experimental data)"
                    elif count >= 5:
                        if "WRITING_ONLY" in tried:
                            required = "FIGURE_CODE_REQUIRED or EXPERIMENT_REQUIRED"
                    elif count >= 3:
                        if "WRITING_ONLY" in tried:
                            required = "Try FIGURE_CODE_REQUIRED"

                escalations[issue_id] = {
                    "count": count,
                    "tried_methods": tried,
                    "banned_methods": banned,
                    "required_escalation": required,
                    "issue_type": issue_type
                }
        return escalations

    def reset_issue_counts(self, reason: str = "manual reset"):
        """Reset issue counters (preserve method history)

        Used to clean up erroneous accumulations caused by bugs

        Args:
            reason: Reason for reset (for logging)
        """
        # Preserve method history, only reset counts
        self.issue_history = {}
        self.last_issues = []
        self.repair_effective = None
        self.stagnation_count = 0
        # Preserve scores and best_score
        self.save()
        return f"Issue counts reset ({reason}). Method history preserved."

    def soft_reset_counts(self, max_count: int = 5):
        """Soft reset: cap all counts at max_count

        Used to fix cases where counts are too high but should not be fully zeroed

        Args:
            max_count: Maximum retained count
        """
        for issue_id in self.issue_history:
            if self.issue_history[issue_id] > max_count:
                self.issue_history[issue_id] = max_count
        self.save()
        return f"Issue counts capped at {max_count}"

    # ==================== Repair Verification ====================

    def record_expected_changes(self, changes: Dict[str, str]):
        """Record expected changes

        Args:
            changes: Dict of file_path -> change_type
                     e.g. {"scripts/create_paper_figures.py": "FIGURE_CODE_REQUIRED"}
        """
        self.expected_changes = changes
        self.save()

    def verify_changes(self, modified_files: List[str]) -> Tuple[bool, List[str]]:
        """Verify whether expected changes occurred

        Args:
            modified_files: List of actually modified files

        Returns:
            (all_verified, missing_files) tuple
        """
        if not self.expected_changes:
            return True, []

        missing = []
        for expected_file in self.expected_changes.keys():
            # Check if file is in the modified list (supports partial matching)
            found = any(expected_file in f or f in expected_file for f in modified_files)
            if not found:
                missing.append(expected_file)

        return len(missing) == 0, missing

    def mark_repair_attempt(self, iteration: int):
        """Mark a self_repair attempt

        Args:
            iteration: Current iteration number
        """
        self.last_repair_iteration = iteration
        self.repair_effective = None  # Pending verification
        self.save()

    def was_last_repair_effective(self) -> Tuple[bool, str]:
        """Check if the last self_repair was effective

        Returns:
            (effective, reason) tuple
        """
        if self.repair_effective is None:
            return True, "No repair attempted yet"
        elif self.repair_effective:
            return True, "Last repair was effective"
        else:
            repeat = self.get_repeat_issues(threshold=2)
            if repeat:
                return False, f"Issues still repeating: {[r[0] for r in repeat[:3]]}"
            return False, "Last repair was ineffective"

    # ==================== Self-Check Report ====================

    def get_self_check_report(self, issue_descriptions: Dict[str, str] = None) -> str:
        """Generate self-check report

        Args:
            issue_descriptions: Optional dict of issue_id -> description
        """
        lines = ["## Self-Check Report\n"]

        # 1. Strategy escalation needs (grouped by issue type)
        escalations = self.get_strategy_escalation(issue_descriptions)
        if escalations:
            # Group: presentation issues vs technical issues
            presentation_issues = {k: v for k, v in escalations.items()
                                   if v.get("issue_type") == "presentation"}
            technical_issues = {k: v for k, v in escalations.items()
                               if v.get("issue_type") == "technical"}

            if presentation_issues:
                lines.append("### Presentation/Layout Issues (use WRITING_ONLY or FIGURE_CODE)")
                lines.append("")
                for issue_id, info in sorted(presentation_issues.items(), key=lambda x: -x[1]["count"]):
                    count = info["count"]
                    tried = info["tried_methods"]
                    required = info["required_escalation"]

                    lines.append(f"**{issue_id}** (repeated {count} times):")
                    if tried:
                        # Count attempts for each method
                        method_counts = {}
                        for m in tried:
                            method_counts[m] = method_counts.get(m, 0) + 1
                        method_str = ", ".join(f"{m}x{c}" for m, c in method_counts.items())
                        lines.append(f"  - Tried: {method_str}")
                    if required:
                        lines.append(f"  - Suggestion: **{required}**")
                    lines.append("")

            if technical_issues:
                lines.append("### Technical Issues (may need EXPERIMENT)")
                lines.append("")
                for issue_id, info in sorted(technical_issues.items(), key=lambda x: -x[1]["count"]):
                    count = info["count"]
                    tried = info["tried_methods"]
                    banned = info["banned_methods"]
                    required = info["required_escalation"]

                    lines.append(f"**{issue_id}** (repeated {count} times):")
                    if tried:
                        method_counts = {}
                        for m in tried:
                            method_counts[m] = method_counts.get(m, 0) + 1
                        method_str = ", ".join(f"{m}x{c}" for m, c in method_counts.items())
                        lines.append(f"  - Tried: {method_str}")
                    if banned:
                        lines.append(f"  - Banned: {', '.join(banned)}")
                    if required:
                        lines.append(f"  - Required: **{required}**")
                    lines.append("")

        # 2. Repeat issue detection (if no escalations)
        repeat_issues = self.get_repeat_issues(threshold=3)
        if repeat_issues and not escalations:
            lines.append("### Recurring Issues (need to change approach!)")
            for issue_id, count in sorted(repeat_issues, key=lambda x: -x[1]):
                lines.append(f"- **{issue_id}**: appeared {count} times")
            lines.append("")

        # 3. Repair effectiveness
        effective, reason = self.was_last_repair_effective()
        if not effective:
            lines.append("### Last Repair Was Ineffective")
            lines.append(f"Reason: {reason}")
            lines.append("Suggestion: Do not repeat the same method; a completely different strategy is needed")
            lines.append("")

        # 4. Expected changes verification
        if self.expected_changes:
            lines.append("### Expected Changes Checklist")
            for f, change_type in self.expected_changes.items():
                lines.append(f"- [ ] {f} ({change_type})")
            lines.append("")

        return "\n".join(lines) if len(lines) > 1 else ""

    # ==================== Meta-Debugger Support ====================

    def get_diagnosis_context(self) -> Dict:
        """Generate context needed for Meta-Debugger diagnosis

        Returns:
            Dictionary containing all diagnosis-related information
        """
        return {
            "scores": {
                "current": self.scores[-1] if self.scores else 0.0,
                "best": self.best_score,
                "recent": self.scores[-10:] if self.scores else [],
                "trend": self._calculate_trend(),
            },
            "stagnation": {
                "count": self.stagnation_count,
                "is_stagnating": self.is_stagnating()[0],
                "reason": self.is_stagnating()[1],
            },
            "issues": {
                "history": self.issue_history,
                "last_issues": self.last_issues,
                "repeat_issues": self.get_repeat_issues(threshold=3),
                "high_repeat": self.get_repeat_issues(threshold=7),
            },
            "repair": {
                "last_iteration": self.last_repair_iteration,
                "effective": self.repair_effective,
                "methods_used": self.issue_repair_methods,
                "expected_changes": self.expected_changes,
            },
            "experiment_empty_count": getattr(self, 'experiment_empty_count', 0),
        }

    def _calculate_trend(self) -> str:
        """Calculate score trend"""
        if len(self.scores) < 2:
            return "insufficient_data"
        recent = self.scores[-5:]
        if len(recent) < 2:
            return "insufficient_data"
        delta = recent[-1] - recent[0]
        if delta > 0.3:
            return "improving"
        elif delta < -0.3:
            return "declining"
        else:
            return "stagnant"

    def get_health_status(self) -> Tuple[str, List[str]]:
        """Get system health status

        Returns:
            (status, reasons) where status is HEALTHY, WARNING, or CRITICAL
        """
        reasons = []

        # Check stagnation
        is_stuck, reason = self.is_stagnating()
        if is_stuck:
            reasons.append(f"Stagnation: {reason}")

        # Check high-repeat issues
        high_repeat = self.get_repeat_issues(threshold=7)
        if high_repeat:
            issue_list = [f"{i[0]}({i[1]}x)" for i in high_repeat[:3]]
            reasons.append(f"High repeat issues: {', '.join(issue_list)}")

        # Check score decline
        if len(self.scores) >= 2 and self.scores[-1] < self.scores[-2] - 0.3:
            reasons.append(f"Score dropped: {self.scores[-2]:.2f} -> {self.scores[-1]:.2f}")

        # Check repair effectiveness
        effective, repair_reason = self.was_last_repair_effective()
        if not effective and "repeating" in repair_reason.lower():
            reasons.append(f"Repair ineffective: {repair_reason}")

        # Check experiment idle runs
        empty_count = getattr(self, 'experiment_empty_count', 0)
        if empty_count >= 2:
            reasons.append(f"Experiments empty: {empty_count} times")

        # Determine status
        if len(reasons) >= 3 or any("High repeat" in r for r in reasons):
            return "CRITICAL", reasons
        elif len(reasons) >= 1:
            return "WARNING", reasons
        else:
            return "HEALTHY", []

    def mark_experiment_empty(self):
        """Mark that an experiment produced empty results"""
        if not hasattr(self, 'experiment_empty_count'):
            self.experiment_empty_count = 0
        self.experiment_empty_count += 1
        self.save()

    def clear_experiment_empty(self):
        """Clear experiment empty result count (when experiment succeeds)"""
        self.experiment_empty_count = 0
        self.save()

    def should_trigger_meta_debug(self) -> Tuple[bool, str]:
        """Determine whether Meta-Debugger should be triggered

        Returns:
            (should_trigger, reason)
        """
        # Condition 1: Stagnation
        if self.stagnation_count >= 3:
            return True, f"stagnation ({self.stagnation_count} iterations)"

        # Condition 2: High issue repetition
        high_repeat = self.get_repeat_issues(threshold=7)
        if high_repeat:
            return True, f"issue_repeat ({high_repeat[0][0]}: {high_repeat[0][1]}x)"

        # Condition 3: Significant score drop
        if len(self.scores) >= 2:
            delta = self.scores[-1] - self.scores[-2]
            if delta <= -0.3:
                return True, f"score_drop ({delta:.2f})"

        # Condition 4: Experiment idle runs
        empty_count = getattr(self, 'experiment_empty_count', 0)
        if empty_count >= 2:
            return True, f"experiment_empty ({empty_count}x)"

        return False, ""

    def reset(self):
        """Reset"""
        self.scores = []
        self.best_score = 0.0
        self.stagnation_count = 0
        self.issue_history = {}
        self.last_issues = []
        self.issue_repair_methods = {}
        self.expected_changes = {}
        self.last_repair_iteration = 0
        self.repair_effective = None
        self.experiment_empty_count = 0
        self.save()


# Legacy interface alias
IterationMemory = SimpleMemory

# Singleton
_memory = None


def get_memory(state_dir: Path = None) -> SimpleMemory:
    """Get global Memory instance"""
    global _memory
    if _memory is None:
        _memory = SimpleMemory(state_dir=state_dir)
    return _memory
