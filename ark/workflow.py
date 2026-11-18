"""Production workflow steps shared by DSH and the rollback runner."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ark.findings_schema import check_claim_prediction, comparison_control_gaps, freeze_comparison_controls
from ark.quality_gate import extract_json


class PaperIteration:
    def __init__(self, project):
        self.project = project
        project._iteration_prep()
        self.state = project.load_paper_state()
        self.current_score = self.state.get("current_score", 0)
        self.resume = project.get_resume_step() if not project._asked_this_iteration else 0
        project.log("", "RAW")
        project.log_section(
            f"Review Phase: Iteration {project.iteration}/{project.max_iterations}  |  "
            f"Score: {self.current_score}/10 → ?  |  Target: {project.paper_accept_threshold}/10"
        )

    def compile(self):
        return "next" if self.project._step_compile(1, 5, self.resume) else "repeat"

    def review(self):
        project = self.project
        self.review_output, self.score, issues = project._step_review(
            2, 5, self.resume, self.state, self.current_score,
        )
        if getattr(project, "_terminal_error", None):
            return self._decision(project._handle_terminal_error(self.score))
        if not self.review_output and self.score == self.current_score and not issues:
            return "repeat"
        stop, self.cleanup, self.stop_after_cleanup = project._check_acceptance(self.score, issues, self.state)
        return "stop" if stop else "next"

    def plan(self):
        project = self.project
        self.action_plan, self.planner_output = project._step_plan(3, 5, self.resume, self.review_output)
        if getattr(project, "_terminal_error", None):
            return self._decision(project._handle_terminal_error(self.score))
        return "next"

    def execute(self):
        project = self.project
        project._step_execute(4, 5, self.resume, self.action_plan, self.planner_output)
        if getattr(project, "_terminal_error", None):
            return self._decision(project._handle_terminal_error(self.score))
        if project._quota_exhausted:
            return self._decision(project._handle_quota_exhausted(self.score))
        return "next"

    def validate(self):
        self.project._step_validate(5, 5, self.resume)
        return "next"

    def finalize(self):
        project = self.project
        result = project._iteration_finalize(
            self.score, self.current_score, self.state, self.review_output,
            self.cleanup, self.stop_after_cleanup,
        )
        if project.hooks and hasattr(project.hooks, "run_paper_iter_end"):
            try:
                project.hooks.run_paper_iter_end(project)
            except Exception as exc:
                project.log(f"hooks.run_paper_iter_end raised: {exc}", "WARN")
        return self._decision(result)

    @staticmethod
    def _decision(continue_run):
        return "repeat" if continue_run else "stop"


@dataclass
class ResearchDecision:
    action: str
    gaps: list[str]

    @classmethod
    def from_review(cls, output, project_root, log):
        review = extract_json(output, want=dict, parse_float=Decimal)
        if not review or type(review.get("sufficient")) is not bool:
            raise ValueError("The completeness review returned no valid yes/no decision.")
        if not {"recommendation", "contradictions", "claim_checks", "gaps"}.issubset(review):
            raise ValueError("The review returned an incomplete claim audit.")
        enough = review["sufficient"]
        action = review["recommendation"]
        if action not in ("proceed_to_writing", "reanalyze_existing", "need_more_experiments"):
            raise ValueError("The completeness review returned an unknown next action.")
        if enough != (action == "proceed_to_writing"):
            raise ValueError("The completeness verdict disagrees with its recommended action.")

        raw_gaps = review["gaps"]
        contradictions = review["contradictions"]
        checks = review["claim_checks"]
        if (not isinstance(raw_gaps, list) or not isinstance(contradictions, list)
                or not all(isinstance(v, str) for v in raw_gaps)
                or not isinstance(checks, list) or not all(isinstance(v, dict) for v in checks)):
            raise ValueError("The review returned an unreadable claim audit.")
        gaps = [v.strip()[:500] for v in raw_gaps if v.strip()][:8]
        corrections = []
        for item in contradictions[:8]:
            if isinstance(item, dict):
                claim = item.get("claim") or item.get("issue") or item.get("note")
                if not isinstance(claim, str) or not claim.strip():
                    raise ValueError("A reported contradiction did not identify the disputed finding.")
                if item.keys() & {"observed_source", "operation", "reference_source", "treatment_source"}:
                    checks = [{**item, "claim": claim}, *checks]
                detail = item.get("note")
                item = f"{item.get('finding_id', '')} {claim}"
                if isinstance(detail, str) and detail.strip() and detail != claim:
                    item += f". {detail}"
            if not isinstance(item, str) or not item.strip():
                raise ValueError("A reported contradiction was empty.")
            corrections.append(item.strip()[:600])
        for check in checks:
            problem = check_claim_prediction(check, project_root)
            if problem:
                severity, message = problem
                detail = f"{str(check.get('claim') or 'Claim check')[:180]}: {message}"[:600]
                if severity == "warn":
                    log(detail, "WARN")
                else:
                    corrections.append(detail)
                    if len(corrections) >= 8:
                        break
        if corrections:
            return cls("correct_findings", corrections)
        if not enough and not gaps:
            reason = review.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("The review requested more research without naming an open question.")
            gaps = [reason.strip()[:500]]
        return cls(action, gaps)


class ExperimentIteration:
    def __init__(self, project, state, iteration, limit, first, idea, findings):
        self.project = project
        self.state = state
        self.iteration = iteration
        self.limit = limit
        self.first = first
        self.idea = idea
        self.findings = findings
        self.resumed = state.get("iteration") == iteration and state.get("status") == "in_progress"
        self.done = list(state.get("steps_done") or []) if self.resumed else []
        if not self.resumed:
            state["reuse_results"] = state.pop("next_action", None) == "reanalyze_existing"
        self.reuse_results = state.get("reuse_results") is True
        state.update(iteration=iteration, status="in_progress", steps_done=self.done)
        state.pop("sufficient", None)
        project._save_dev_phase_state(state)
        project.log("", "RAW")
        project.log_section(f"Dev Phase: Iteration {iteration}/{limit}")
        project._send_dev_phase_telegram("iteration", iteration, limit)

    def remaining_steps(self):
        steps = ("analyze",) if self.reuse_results else ("plan", "run", "analyze")
        return [step for step in steps if step not in self.done]

    def _finished(self, step):
        self.done.append(step)
        self.project._save_dev_phase_state(self.state)

    def plan(self):
        project = self.project
        context = self.findings
        gaps = self.state.get("evidence_gaps")
        if gaps:
            context += "\nOpen questions from the previous evaluation:\n" + "\n".join(f"- {gap}" for gap in gaps)
        project._plan_experiments(self.iteration, self.limit, self.idea, context)
        if project._abort_dev_on_terminal_error("planning experiments"):
            return False
        if self.first:
            project._experiment_approval_gate()
        state_dir = getattr(project, "state_dir", None)
        plan = state_dir / "experiment_plan.yaml" if state_dir is not None else None
        try:
            self.state["comparison_controls"] = freeze_comparison_controls(
                plan, self.state.get("comparison_controls", []), getattr(project, "code_dir", None),
            )
        except ValueError as exc:
            project._terminal_error = f"Cannot checkpoint the planned comparison controls. {exc}"
            project.log(project._terminal_error, "ERROR")
            return False
        self._finished("plan")
        return True

    def run(self):
        project = self.project
        project._run_experiments(self.iteration, self.limit, resumed=self.resumed)
        if project._abort_dev_on_terminal_error("running experiments"):
            return False
        self._finished("run")
        return True

    def analyze(self):
        project = self.project
        corrections = self.state.get("analysis_corrections")
        if corrections or self.reuse_results:
            project._analyze_results(corrections or self.state.get("evidence_gaps") or [])
        else:
            project._analyze_results()
        if project._abort_dev_on_terminal_error("analyzing results"):
            return False
        self._finished("analyze")
        return True

    def evaluate(self):
        project = self.project
        # Allow one analysis repair from the same observations, even at the
        # iteration limit. Persist the invalidation before calling any model.
        while True:
            self.findings = project._load_findings_summary()
            decision = project._evaluate_completeness(self.idea, self.findings)
            if decision is None:
                return None
            if decision.action != "correct_findings":
                # Use the pre-run checkpoint on Continue and on analysis-only
                # iterations. A later edit of the plan cannot change the check.
                control_gaps = comparison_control_gaps(
                    self.state.get("comparison_controls", []), getattr(project, "code_dir", None),
                )
                if control_gaps:
                    action = "need_more_experiments" if decision.action == "proceed_to_writing" else decision.action
                    decision = ResearchDecision(action, list(dict.fromkeys([*control_gaps, *decision.gaps])))
            attempted = bool(self.state.get("analysis_corrections"))
            # A qualified yes may contain a necessary scope correction that
            # the model placed in gaps. Reconcile it with the findings once.
            qualified_yes = decision.action == "proceed_to_writing" and decision.gaps and not attempted
            if decision.action != "correct_findings" and not qualified_yes:
                break
            self.state["analysis_corrections"] = decision.gaps
            if "analyze" in self.done:
                self.done.remove("analyze")
            project._save_dev_phase_state(self.state)
            project.log_step("The claim audit needs a correction: " + decision.gaps[0], "warning")
            if attempted:
                project._terminal_error = "The findings still contradict the claim audit. " + decision.gaps[0]
                return None
            if not self.analyze():
                return None
        self.state.pop("analysis_corrections", None)
        sufficient = decision.action == "proceed_to_writing"
        self.state["next_action"] = decision.action
        self.state["evidence_gaps"] = decision.gaps
        self.state["sufficient"] = sufficient
        self._finished("evaluate")
        if project.hooks and hasattr(project.hooks, "run_dev_iter_end"):
            try:
                project.hooks.run_dev_iter_end(project, dev_iter=self.iteration)
            except Exception as exc:
                project.log(f"hooks.run_dev_iter_end raised: {exc}", "WARN")
        if sufficient:
            project.log_step("Experiments sufficient, proceeding to initial draft", "success")
        elif self.iteration == self.limit:
            project.log_step("Experiment budget reached. Drafting with the recorded limitations.", "warning")
        elif decision.action == "reanalyze_existing":
            project.log_step("The next analysis will use the saved measurements.", "info")
        else:
            project.log_step(f"Dev iter {self.iteration}: more experiments needed", "warning")
        return sufficient


class WorkflowSteps:
    """Per-run Python state and actions called by the DSH workflow plugin."""

    def __init__(self, project):
        self.project = project
        self.iterations_at_start = None
        self.actions = {
            "setup": project._prepare_research_workspace,
            "should-research": project._should_run_research_phase,
            "research": project._run_research_phase,
            "prepare": self.prepare,
            "dev-start": self.start_dev,
            "dev-iteration": self.start_experiment,
            "dev-plan": lambda: self.experiment.plan(),
            "dev-run": lambda: self.experiment.run(),
            "dev-analyze": lambda: self.experiment.analyze(),
            "dev-evaluate": self.evaluate_experiment,
            "dev-writing": project._start_initial_draft,
            "dev-figures": project._generate_all_figures,
            "dev-draft": lambda: project._write_initial_draft(self.idea),
            "dev-deliver": lambda: project._deliver_dev_phase(self.dev_state, self.dev_limit),
            "dev-check": self.check_dev,
            "paper-start": self.start_paper,
            "paper-compile": lambda: self.paper.compile(),
            "paper-review": lambda: self.paper.review(),
            "paper-plan": lambda: self.paper.plan(),
            "paper-execute": lambda: self.paper.execute(),
            "paper-validate": lambda: self.paper.validate(),
            "paper-finalize": lambda: self.paper.finalize(),
        }

    def prepare(self):
        self.target = self.project._get_max_iteration_target()
        self.iterations_at_start = self.project.iteration
        return self.project._should_run_dev_phase()

    def start_dev(self):
        self.dev_state, self.dev_start, self.dev_limit = self.project._begin_dev_phase()
        self.dev_iter = self.dev_start
        self.idea = self.project._research_idea
        self.findings = self.project._load_findings_summary()

    def start_experiment(self):
        if self.dev_iter >= self.dev_limit:
            return None
        self.dev_iter += 1
        self.experiment = ExperimentIteration(
            self.project, self.dev_state, self.dev_iter, self.dev_limit,
            self.dev_iter == self.dev_start + 1, self.idea, self.findings,
        )
        return self.experiment.remaining_steps()

    def evaluate_experiment(self):
        sufficient = self.experiment.evaluate()
        self.findings = self.experiment.findings
        return sufficient

    def check_dev(self):
        if getattr(self.project, "_terminal_error", None):
            self.project._handle_terminal_error(0.0, rollback_iteration=False)
            return False
        return True

    def start_paper(self):
        project = self.project
        if datetime.now() >= project.max_end_time or project.iteration >= self.target or project._stop_requested:
            return False
        self.paper = PaperIteration(project)
        return True
