"""Pydantic models for dashboard API responses."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AgentActivity(BaseModel):
    type: str = ""
    start_time: str = ""
    elapsed_str: str = ""


class LiveActivity(BaseModel):
    iteration: str = ""
    phase: str = ""
    agent: Optional[AgentActivity] = None
    rate_limit: str = ""
    recent_lines: List[str] = Field(default_factory=list)


class Issue(BaseModel):
    id: str = ""
    title: str = ""
    description: str = ""
    status: str = "pending"
    type: str = ""
    actions: List[Dict[str, Any]] = Field(default_factory=list)


class CostAgent(BaseModel):
    calls: int = 0
    seconds: float = 0
    tokens: int = 0


class CostReport(BaseModel):
    per_agent: Dict[str, CostAgent] = Field(default_factory=dict)
    total_tokens: int = 0
    total_seconds: float = 0


class ProjectSummary(BaseModel):
    name: str
    title: str = ""
    venue: str = ""
    model: str = ""
    running: bool = False
    pid: Optional[int] = None
    current_score: Optional[float] = None
    best_score: Optional[float] = None
    scores: List[float] = Field(default_factory=list)
    iteration: str = ""
    phase: str = ""
    active_agent: Optional[str] = None
    stagnation_count: int = 0
    acceptance_threshold: Optional[float] = None


class ProjectDetail(BaseModel):
    summary: ProjectSummary
    checkpoint: Dict[str, Any] = Field(default_factory=dict)
    memory: Dict[str, Any] = Field(default_factory=dict)
    action_plan: Dict[str, Any] = Field(default_factory=dict)
    issues: List[Issue] = Field(default_factory=list)
    cost_report: CostReport = Field(default_factory=CostReport)
    live: LiveActivity = Field(default_factory=LiveActivity)
    latest_review_md: str = ""
    findings_count: int = 0
    log_file: str = ""
