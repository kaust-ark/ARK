"""Read project state from disk — YAML files, logs, PDFs."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .models import (
    AgentActivity,
    CostAgent,
    CostReport,
    Issue,
    LiveActivity,
    ProjectDetail,
    ProjectSummary,
)


# ---------------------------------------------------------------------------
# Helpers (adapted from ark.cli)
# ---------------------------------------------------------------------------

def get_ark_root() -> Path:
    pkg_root = Path(__file__).parent.parent.parent.absolute()
    if (pkg_root / "projects").exists():
        return pkg_root
    if (pkg_root / "pyproject.toml").exists():
        (pkg_root / "projects").mkdir(exist_ok=True)
        return pkg_root
    home_ark = Path.home() / ".ark"
    home_ark.mkdir(exist_ok=True)
    (home_ark / "projects").mkdir(exist_ok=True)
    return home_ark


def get_projects_dir() -> Path:
    return get_ark_root() / "projects"


def _safe_yaml(path: Path) -> dict:
    """Load YAML, never crash."""
    try:
        if path.exists():
            with open(path) as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}


def _is_running(project_dir: Path) -> Tuple[bool, Optional[int]]:
    pid_file = project_dir / ".pid"
    if not pid_file.exists():
        return False, None
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True, pid
    except (ProcessLookupError, ValueError, PermissionError):
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass
        return False, None


def _get_latest_log(logs_dir: Path) -> Optional[Path]:
    """Return the most recently modified .log file."""
    if not logs_dir.is_dir():
        return None
    logs = sorted(logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def _tail_lines(path: Path, n: int = 300) -> List[str]:
    """Read last n lines of a file efficiently."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = min(size, n * 200)
            f.seek(max(0, size - block))
            data = f.read().decode("utf-8", errors="replace")
            return data.splitlines()[-n:]
    except Exception:
        return []


def _parse_live_info(lines: List[str]) -> dict:
    """Parse log lines to extract live execution info."""
    info: Dict[str, Any] = {"iteration": "", "phase": "", "agent": None, "rate_limit": ""}

    last_agent_start = None
    last_agent_complete = None

    for i, line in enumerate(lines):
        m = re.search(r"ITERATION\s+(\d+/\d+)", line)
        if m:
            info["iteration"] = m.group(1)

        m = re.search(r"PHASE\s+(\d+/\d+):\s*(.+?)[\s\u2500]*$", line)
        if m:
            info["phase"] = f"Phase {m.group(1)}: {m.group(2).strip()}"

        m = re.search(r"\[(\d{2}:\d{2}:\d{2})\].*?Agent\s+\[(\w+)\]\s*\u2192", line)
        if m:
            last_agent_start = (m.group(2), m.group(1), i)

        m = re.search(r"Agent\s+\[(\w+)\]\s+completed", line)
        if m:
            last_agent_complete = (m.group(1), i)

        m = re.search(r"Rate Limit.*?waiting\s*([\d.]+)\s*minutes", line)
        if m:
            info["rate_limit"] = f"Rate limited, waiting {m.group(1)}min"
        m = re.search(r"waiting\s*([\d.]+)\s*minutes before auto-recovery", line)
        if m:
            info["rate_limit"] = f"Rate limited, resuming in {m.group(1)}min"

    if last_agent_start:
        agent_type, start_ts, start_idx = last_agent_start
        is_active = True
        if last_agent_complete:
            _, comp_idx = last_agent_complete
            if comp_idx > start_idx:
                is_active = False
        if is_active:
            elapsed_str = ""
            try:
                now = datetime.now()
                h, mv, s = map(int, start_ts.split(":"))
                start_dt = now.replace(hour=h, minute=mv, second=s, microsecond=0)
                if start_dt > now:
                    start_dt = start_dt.replace(day=start_dt.day - 1)
                elapsed = (now - start_dt).total_seconds()
                if elapsed < 60:
                    elapsed_str = f"{int(elapsed)}s"
                elif elapsed < 3600:
                    elapsed_str = f"{int(elapsed // 60)}m{int(elapsed % 60)}s"
                else:
                    elapsed_str = f"{int(elapsed // 3600)}h{int((elapsed % 3600) // 60)}m"
            except Exception:
                pass
            info["agent"] = {"type": agent_type, "start_time": start_ts, "elapsed_str": elapsed_str}

    return info


# ---------------------------------------------------------------------------
# Public readers
# ---------------------------------------------------------------------------

def list_project_names() -> List[str]:
    pdir = get_projects_dir()
    if not pdir.is_dir():
        return []
    return sorted(
        d.name for d in pdir.iterdir()
        if d.is_dir() and (d / "config.yaml").exists()
    )


def _state_dir(config: dict) -> Optional[Path]:
    code_dir = config.get("code_dir")
    if not code_dir:
        return None
    sd = Path(code_dir) / "auto_research" / "state"
    return sd if sd.is_dir() else None


def _logs_dir(config: dict) -> Optional[Path]:
    code_dir = config.get("code_dir")
    if not code_dir:
        return None
    ld = Path(code_dir) / "auto_research" / "logs"
    return ld if ld.is_dir() else None


def read_project_summary(name: str) -> ProjectSummary:
    pdir = get_projects_dir() / name
    config = _safe_yaml(pdir / "config.yaml")
    running, pid = _is_running(pdir)

    sd = _state_dir(config)
    memory = _safe_yaml(sd / "memory.yaml") if sd else {}
    checkpoint = _safe_yaml(sd / "checkpoint.yaml") if sd else {}

    scores = memory.get("scores", []) or []
    scores = [s for s in scores if isinstance(s, (int, float))]

    # Live info from logs
    ld = _logs_dir(config)
    log_path = _get_latest_log(ld) if ld else None
    live_info: Dict[str, Any] = {}
    if log_path and running:
        lines = _tail_lines(log_path, 300)
        live_info = _parse_live_info(lines)

    active_agent = None
    if live_info.get("agent"):
        active_agent = live_info["agent"].get("type", "")

    iteration = live_info.get("iteration", "")
    if not iteration and checkpoint:
        it = checkpoint.get("iteration", "")
        if it:
            iteration = str(it)

    return ProjectSummary(
        name=name,
        title=config.get("title", ""),
        venue=config.get("venue", ""),
        model=config.get("model", ""),
        running=running,
        pid=pid,
        current_score=scores[-1] if scores else None,
        best_score=memory.get("best_score"),
        scores=scores,
        iteration=iteration,
        phase=live_info.get("phase", ""),
        active_agent=active_agent,
        stagnation_count=memory.get("stagnation_count", 0) or 0,
        acceptance_threshold=config.get("paper_accept_threshold"),
    )


def read_project_detail(name: str) -> ProjectDetail:
    pdir = get_projects_dir() / name
    config = _safe_yaml(pdir / "config.yaml")
    summary = read_project_summary(name)

    sd = _state_dir(config)
    memory = _safe_yaml(sd / "memory.yaml") if sd else {}
    checkpoint = _safe_yaml(sd / "checkpoint.yaml") if sd else {}
    action_plan = _safe_yaml(sd / "action_plan.yaml") if sd else {}
    findings = _safe_yaml(sd / "findings.yaml") if sd else {}

    # Issues
    raw_issues = action_plan.get("issues", []) or []
    raw_issues = [i for i in raw_issues if i is not None and isinstance(i, dict)]
    issues = [
        Issue(
            id=i.get("id", ""),
            title=i.get("title") or "",
            description=i.get("description") or "",
            status=i.get("status") or "pending",
            type=i.get("type") or "",
            actions=i.get("actions") or [],
        )
        for i in raw_issues
    ]

    # Cost report
    cost = CostReport()
    cost_data = _safe_yaml(sd / "cost_report.yaml") if sd else {}
    if cost_data:
        pa = cost_data.get("per_agent", {}) or {}
        for agent_name, stats in pa.items():
            if isinstance(stats, dict):
                cost.per_agent[agent_name] = CostAgent(
                    calls=stats.get("calls", 0) or 0,
                    seconds=stats.get("seconds", 0) or 0,
                    tokens=stats.get("tokens", 0) or 0,
                )
        cost.total_tokens = cost_data.get("total_tokens", 0) or 0
        cost.total_seconds = cost_data.get("total_seconds", 0) or 0

    # Live info
    ld = _logs_dir(config)
    log_path = _get_latest_log(ld) if ld else None
    live = LiveActivity()
    log_file = ""
    if log_path:
        log_file = str(log_path)
        lines = _tail_lines(log_path, 300)
        info = _parse_live_info(lines)
        live.iteration = info.get("iteration", "")
        live.phase = info.get("phase", "")
        live.rate_limit = info.get("rate_limit", "")
        live.recent_lines = lines[-30:]
        if info.get("agent"):
            live.agent = AgentActivity(**info["agent"])

    # Review
    review_md = ""
    if sd and (sd / "latest_review.md").exists():
        try:
            review_md = (sd / "latest_review.md").read_text(errors="replace")
        except Exception:
            pass

    # Findings count
    findings_count = 0
    if isinstance(findings, dict):
        findings_count = len(findings)
    elif isinstance(findings, list):
        findings_count = len(findings)

    return ProjectDetail(
        summary=summary,
        checkpoint=checkpoint,
        memory=memory,
        action_plan=action_plan,
        issues=issues,
        cost_report=cost,
        live=live,
        latest_review_md=review_md,
        findings_count=findings_count,
        log_file=log_file,
    )


def read_log_lines(name: str, n: int = 100) -> Tuple[List[str], str]:
    """Return last n lines of the project's latest log."""
    pdir = get_projects_dir() / name
    config = _safe_yaml(pdir / "config.yaml")
    ld = _logs_dir(config)
    log_path = _get_latest_log(ld) if ld else None
    if not log_path:
        return [], ""
    return _tail_lines(log_path, n), str(log_path)


def get_pdf_path(name: str) -> Optional[Path]:
    """Return path to latest compiled PDF."""
    pdir = get_projects_dir() / name
    config = _safe_yaml(pdir / "config.yaml")
    code_dir = config.get("code_dir")
    if not code_dir:
        return None
    latex_dir = config.get("latex_dir", "paper")
    pdf_dir = Path(code_dir) / latex_dir
    if not pdf_dir.is_dir():
        return None
    pdfs = sorted(pdf_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    return pdfs[0] if pdfs else None


def get_file_mtimes(name: str) -> Dict[str, float]:
    """Return mtime of all watched state files for change detection."""
    pdir = get_projects_dir() / name
    config = _safe_yaml(pdir / "config.yaml")
    sd = _state_dir(config)
    ld = _logs_dir(config)
    mtimes: Dict[str, float] = {}

    if sd:
        for fname in [
            "memory.yaml", "checkpoint.yaml", "action_plan.yaml",
            "latest_review.md", "findings.yaml", "cost_report.yaml",
        ]:
            fp = sd / fname
            if fp.exists():
                try:
                    mtimes[str(fp)] = fp.stat().st_mtime
                except Exception:
                    pass

    if ld:
        log_path = _get_latest_log(ld)
        if log_path:
            try:
                mtimes[str(log_path)] = log_path.stat().st_mtime
            except Exception:
                pass

    pid_file = pdir / ".pid"
    if pid_file.exists():
        try:
            mtimes[str(pid_file)] = pid_file.stat().st_mtime
        except Exception:
            pass

    return mtimes
