"""Bind the Room team router to a live ARK ``Orchestrator``.

``run_room_team(orch)`` replaces ARK's fixed review loop with the Room loop
from :mod:`ark.sharednet.team`: the same six agents, the same prompt files and
state files, but every hand-off is a typed message in a SharedNet Room and the
Agent that just finished says who works next.

Configuration (``config.yaml`` of the project, or ``SHAREDNET_INVITE`` in the
environment, which wins):

    sharednet:
      invite: "ROOM=rom_xxx TOKEN=rit_xxx BASE=https://sharednet.ai"   # pasted from the Web
      goal: "optional; defaults to the project's goal anchor / idea"
      start_role: writer          # default: writer when nothing was reviewed yet, else reviewer
      max_hops: 12
      roles: [experimenter, coder, writer, reviewer, planner]
      steer_wait_seconds: 0       # >0: wait this long for humans to speak before each hop

What stays ARK's: prompts, ``run_agent`` (OpenHands), cost tracking, the
research phase, compile, score parsing, the acceptance threshold. What the Room
adds: distinct members per role, ``work.request`` / ``work.result`` /
``done`` / ``stopped`` messages, human guidance between hops, and resume from
the Room log.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import yaml

from ark.config import defaults

from .room import Invite
from .team import ARK_ROLES, HopContext, RoomTeam, TeamResult

TIMEOUTS = {
    "researcher": defaults.TIMEOUT_RESEARCHER,
    "reviewer": defaults.TIMEOUT_REVIEWER,
    "planner": defaults.TIMEOUT_PLANNER,
    "writer": defaults.TIMEOUT_WRITER,
    "experimenter": defaults.TIMEOUT_EXPERIMENTER,
    "coder": defaults.TIMEOUT_CODER,
}

WORKING_ROLES: tuple[str, ...] = ("experimenter", "coder", "writer", "reviewer", "planner")

# Roles that read the figures on disk: the writer cites them, the reviewer
# judges the compiled PDF. The figure phase runs before each of them, which is
# where the fixed loop puts it too (before the initial draft, and at the end of
# every iteration — i.e. before the next review).
FIGURE_CONSUMING_ROLES: tuple[str, ...] = ("writer", "reviewer")


def sharednet_settings(config: dict) -> Optional[dict]:
    """The ``sharednet:`` block, with ``SHAREDNET_INVITE`` overriding the invite."""
    block = config.get("sharednet")
    invite_env = os.environ.get("SHAREDNET_INVITE", "").strip()
    if not block and not invite_env:
        return None
    settings = dict(block or {})
    if invite_env:
        settings["invite"] = invite_env
    return settings


class ArkRoomTeam:
    """Role-specific tasks and guards on top of the generic router."""

    def __init__(self, orch, settings: dict):
        self.orch = orch
        self.settings = settings
        invite_text = settings.get("invite")
        if invite_text:
            invite = Invite.parse(str(invite_text))
        else:
            invite = Invite(base_url=settings["base_url"], room_id=settings["room_id"], token=settings["token"])
        self.invite = invite
        roles = tuple(settings.get("roles") or WORKING_ROLES)
        unknown = [role for role in roles if role not in ARK_ROLES]
        if unknown:
            raise ValueError(f"unknown roles in sharednet.roles: {unknown}")
        self.team = RoomTeam(
            invite.base_url,
            invite.room_id,
            invite.token,
            self.run_agent,
            roles=roles,
            coordinator_name=str(settings.get("coordinator_name") or f"ark:{orch.project_name}"),
            member_prefix=str(settings.get("member_prefix") or ""),
            task_builder=self.task_for,
            done_guard=self.done_guard,
            max_hops=int(settings.get("max_hops", 12)),
            steer_wait_seconds=int(settings.get("steer_wait_seconds", 0)),
            log=lambda line: orch.log(line, "INFO"),
        )
        self.last_score: Optional[float] = None

    # ── Goal / start ─────────────────────────────────────────────────────────
    def goal(self) -> str:
        explicit = self.settings.get("goal")
        if explicit:
            return str(explicit)
        anchor = self.orch.config.get("goal_anchor")
        if anchor:
            return str(anchor)
        idea = self.orch.state_dir / "idea.md"
        if idea.exists():
            return idea.read_text()[:2000]
        return f"Produce a submission-ready paper for project {self.orch.project_name}."

    def start_role(self) -> str:
        explicit = self.settings.get("start_role")
        if explicit:
            return str(explicit)
        reviewed = bool(self.orch.load_paper_state().get("reviews"))
        return "reviewer" if reviewed else "writer"

    # ── run_agent with ARK's timeouts and score bookkeeping ─────────────────
    def run_agent(self, role: str, task: str) -> str:
        if role in FIGURE_CONSUMING_ROLES:
            self._refresh_figures(role)
        output = self.orch.run_agent(role, task, timeout=TIMEOUTS.get(role, 1800))
        if role == "reviewer":
            self._record_review(output)
        return output

    def _refresh_figures(self, role: str) -> None:
        """Run ARK's figure phase, as the fixed loop does, before the roles that
        read figures.

        The fixed loop refreshes figures twice: ``_generate_all_figures`` before
        the initial draft, and ``_run_figure_phase`` in ``_step_validate`` at the
        end of every iteration — matplotlib regeneration, overlap detection, AI
        concept figures (``figure_generation: nano_banana``), then compile. The
        Room loop calls neither, so without this a RAC run ships with stale or
        missing figures and no concept figure at all, and a comparison against
        the fixed loop measures the missing wiring rather than the coordination.
        Ends in a compile, which is what the reviewer needs anyway.
        """
        orch = self.orch
        try:
            if orch._should_skip_figure_phase():
                orch.log_step("Figure phase skipped", "info")
                orch.compile_latex()
            else:
                orch._run_figure_phase()  # step 6 of that phase is the compile
        except Exception as error:  # a missing draft or a bad figure must not end the run
            orch.log(f"figure phase before {role} failed: {error}", "WARN")

    def _record_review(self, output: str) -> None:
        orch = self.orch
        score = orch.parse_review_score(output)
        self.last_score = score
        paper_state = orch.load_paper_state()
        paper_state.setdefault("reviews", []).append({
            "iteration": len(paper_state.get("reviews", [])) + 1,
            "timestamp": datetime.now().isoformat(),
            "score": score,
            "room": self.invite.room_id,
        })
        paper_state["current_score"] = score
        orch.save_paper_state(paper_state)
        try:
            orch.memory.record_score(score)
        except Exception:
            pass
        orch.log_step(f"Score: {score}/10 (threshold {orch.paper_accept_threshold}/10)",
                      "success" if score >= orch.paper_accept_threshold else "warning")

    # ── Only the reviewer's verdict can close the run ────────────────────────
    def done_guard(self, role: str, output: str) -> Optional[bool]:
        if role != "reviewer":
            return False
        score = self.orch.parse_review_score(output)
        return score >= self.orch.paper_accept_threshold

    # ── Tasks per role, from the shared state files ─────────────────────────
    def task_for(self, context: HopContext) -> str:
        orch = self.orch
        latex_dir = orch.config.get("latex_dir", "paper")
        venue = orch.config.get("venue", "top venue")
        handed = ""
        if context.previous_role:
            reason = context.previous_decision.reason if context.previous_decision else ""
            handed = f"\nHanded to you by {context.previous_role}" + (f": {reason}" if reason else ".") + "\n"
        head = f"## Goal\n{context.goal}\n{handed}"

        if context.role == "reviewer":
            return (
                f"{head}\nReview the current paper {latex_dir}/main.tex and {latex_dir}/main.pdf "
                f"according to {venue} standards. Output a detailed review report with an "
                f"`Overall Score: N/10` line and Major/Minor issues, and save it to "
                f"auto_research/state/latest_review.md. Acceptance threshold: "
                f"{orch.paper_accept_threshold}/10. Say done only if the paper is at or above it."
            )
        if context.role == "planner":
            review = self._read(orch.state_dir / "latest_review.md")
            return (
                f"{head}\nRead auto_research/state/latest_review.md (latest score: "
                f"{self.last_score if self.last_score is not None else 'unknown'}/10) and write "
                f"auto_research/state/action_plan.yaml classifying every Major issue as "
                f"EXPERIMENT_REQUIRED, FIGURE_CODE_REQUIRED, LITERATURE_REQUIRED or WRITING_ONLY. "
                f"Then hand off to the role whose work comes first: experimenter for new data, "
                f"coder for figure scripts, writer when only LaTeX changes are needed.\n\n"
                f"### Latest review (excerpt)\n{review[:4000]}"
            )
        if context.role == "writer":
            plan = self._plan_issues(("WRITING_ONLY", "LITERATURE_REQUIRED"))
            if not orch.load_paper_state().get("reviews") and not plan:
                findings = self._safe(orch._load_findings_summary)
                figures = self._safe(orch._list_available_figures)
                return (
                    f"{head}\nWrite a complete, submission-ready draft in {latex_dir}/main.tex from the "
                    f"research idea (auto_research/state/idea.md), the findings below and the figures "
                    f"already generated. Compile with pdflatex before you finish.\n\n"
                    f"### Findings\n{findings[:6000]}\n\n### Figures\n{figures[:2000]}"
                )
            return (
                f"{head}\nRevise {latex_dir}/main.tex to resolve the writing tasks in "
                f"auto_research/state/action_plan.yaml, integrate any new results from "
                f"auto_research/state/findings.yaml, keep the body within the venue page limit, and "
                f"compile before you finish.\n\n### Tasks\n{plan or '(read the action plan file)'}"
            )
        if context.role == "experimenter":
            plan = self._plan_issues(("EXPERIMENT_REQUIRED",))
            return (
                f"{head}\nRun the experiments that are still missing, on real systems and data, and "
                f"record results in auto_research/state/findings.yaml with a `coverage:` field per "
                f"item of the Experimental Protocol in auto_research/state/project_context.md.\n\n"
                f"### Experiment tasks\n{plan or 'Everything in the Experimental Protocol not yet covered.'}"
            )
        if context.role == "coder":
            plan = self._plan_issues(("FIGURE_CODE_REQUIRED",))
            return (
                f"{head}\nImplement the code changes below (figure scripts, analysis scripts, experiment "
                f"code) and run them so their outputs exist on disk.\n\n### Tasks\n"
                f"{plan or (context.previous_decision.reason if context.previous_decision else '(see the previous message in the Room)')}"
            )
        if context.role == "researcher":
            return (
                f"{head}\nAnalyze the proposal, refresh auto_research/state/idea.md and "
                f"auto_research/state/project_context.md, and state the Experimental Protocol."
            )
        return f"{head}\nDo the {context.role}'s part of the work now."

    # ── helpers ─────────────────────────────────────────────────────────────
    def _plan_issues(self, kinds: tuple[str, ...]) -> str:
        try:
            plan = self.orch._load_action_plan()
        except Exception:
            return ""
        lines = []
        for issue in plan.get("issues", []) or []:
            if not isinstance(issue, dict) or issue.get("type") not in kinds:
                continue
            if issue.get("status") in ("completed", "skipped"):
                continue
            lines.append(f"- {issue.get('id', '?')} [{issue.get('type')}] {issue.get('title', '')}: "
                         f"{(issue.get('description') or '')[:400]}")
        return "\n".join(lines)

    @staticmethod
    def _read(path) -> str:
        try:
            return path.read_text()
        except Exception:
            return ""

    @staticmethod
    def _safe(fn) -> str:
        try:
            return str(fn() or "")
        except Exception:
            return ""


def run_room_team(orch, settings: Optional[dict] = None) -> TeamResult:
    """Entry point used by ``Orchestrator.run`` and ``python -m ark.sharednet``."""
    settings = settings or sharednet_settings(orch.config)
    if not settings:
        raise ValueError("no sharednet settings: add a `sharednet:` block to config.yaml or set SHAREDNET_INVITE")
    team = ArkRoomTeam(orch, settings)
    orch.log_section(f"SharedNet Room team  |  {team.invite.room_id}  |  roles: {', '.join(team.team.roles)}")
    result = team.team.run(team.goal(), start_role=team.start_role())
    orch.log_section(f"Room team {'finished' if result.done else 'stopped'}: {result.reason}  |  route: {' → '.join(result.route) or '-'}")
    if result.done and team.last_score is not None and team.last_score >= orch.paper_accept_threshold:
        state = orch.load_paper_state()
        state["status"] = "accepted"
        state["accepted_score"] = team.last_score
        orch.save_paper_state(state)
    try:
        orch.notify_progress("Room team", result.reason, level="done" if result.done else "warn")
    except Exception:
        pass
    _write_room_summary(orch, team, result)
    return result


def _write_room_summary(orch, team: ArkRoomTeam, result: TeamResult) -> None:
    """A small YAML next to the other state files: where the Room is and what happened."""
    try:
        summary = {
            "base_url": team.invite.base_url,
            "room_id": team.invite.room_id,
            "done": result.done,
            "reason": result.reason,
            "route": result.route,
            "hops": [{"hop": h.hop, "role": h.role, "next": h.decision.next, "done": h.decision.done,
                      "decided_by": h.decision.decided_by, "reason": h.decision.reason,
                      "request_seq": h.request.sequence, "result_seq": h.result.sequence}
                     for h in result.hops],
            "updated_at": datetime.now().isoformat(),
        }
        (orch.state_dir / "sharednet_room.yaml").write_text(
            yaml.dump(summary, default_flow_style=False, allow_unicode=True))
    except Exception as error:
        orch.log(f"could not write sharednet_room.yaml: {error}", "WARN")
