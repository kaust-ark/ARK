"""Production workflow steps shared by DSH and the rollback runner."""

from datetime import datetime


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
        state.update(iteration=iteration, status="in_progress", steps_done=self.done)
        state.pop("sufficient", None)
        project._save_dev_phase_state(state)
        project.log("", "RAW")
        project.log_section(f"Dev Phase: Iteration {iteration}/{limit}")
        project._send_dev_phase_telegram("iteration", iteration, limit)

    def _finished(self, step):
        self.done.append(step)
        self.project._save_dev_phase_state(self.state)

    def plan(self):
        project = self.project
        project._plan_experiments(self.iteration, self.limit, self.idea, self.findings)
        if project._abort_dev_on_terminal_error("planning experiments"):
            return False
        if self.first:
            project._experiment_approval_gate()
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
        project._analyze_results()
        if project._abort_dev_on_terminal_error("analyzing results"):
            return False
        self._finished("analyze")
        return True

    def evaluate(self):
        project = self.project
        self.findings = project._load_findings_summary()
        sufficient = bool(project._evaluate_completeness(self.idea, self.findings))
        self.state["sufficient"] = sufficient
        self._finished("evaluate")
        if project.hooks and hasattr(project.hooks, "run_dev_iter_end"):
            try:
                project.hooks.run_dev_iter_end(project, dev_iter=self.iteration)
            except Exception as exc:
                project.log(f"hooks.run_dev_iter_end raised: {exc}", "WARN")
        if sufficient:
            project.log_step("Experiments sufficient, proceeding to initial draft", "success")
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
        return self.experiment.done

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
