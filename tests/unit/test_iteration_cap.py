"""max_iterations must be a cumulative cap, not per-run increment.

Context: the webapp's Continue API writes `max_iterations = existing +
additional` to DB — a cumulative total across the project's lifetime.
Historically the pipeline did `target = iteration + max_iterations`,
which treated the DB value as ADDITIONAL, so after a continue the
run was capped far higher than the user asked for. Visible tell was
the status header reading "Iteration 11/8".
"""

import pytest
from ark.pipeline import PipelineMixin

class _PipelineTargetTester(PipelineMixin):
    """Subclass to test the real method from PipelineMixin."""
    def __init__(self, iteration: int, max_iterations: int):
        self.iteration = iteration
        self.max_iterations = max_iterations
    
    def log(self, *args, **kwargs):
        pass # PipelineMixin requires self.log

class TestIterationCap:
    def test_fresh_run(self):
        """Fresh project, max=2 → cap 2 iterations."""
        orch = _PipelineTargetTester(iteration=0, max_iterations=2)
        assert orch._get_max_iteration_target() == 2

    def test_continue_after_prior_iterations(self):
        """Continue +3 after 5 iters done → DB now 8, loop runs iter 6..8."""
        orch = _PipelineTargetTester(iteration=5, max_iterations=8)
        assert orch._get_max_iteration_target() == 8
        iters_remaining = orch._get_max_iteration_target() - orch.iteration
        assert iters_remaining == 3, (
            "User requested +3 via Continue; loop should run exactly 3 more. "
            f"Got cap {orch._get_max_iteration_target()} with iter={orch.iteration} → "
            f"{iters_remaining} iterations."
        )

    def test_continue_after_inconsistent_state(self):
        """If somehow iter > max (shouldn't happen, but defensive), cap
        does not go backward."""
        orch = _PipelineTargetTester(iteration=10, max_iterations=8)
        assert orch._get_max_iteration_target() == 10, "cap must not drop below current iter"

    def test_compounded_continues(self):
        """Two successive continues each +3 → DB goes 2 → 5 → 8."""
        # first continue after fresh 2-iter run
        orch = _PipelineTargetTester(iteration=2, max_iterations=5)
        assert orch._get_max_iteration_target() - orch.iteration == 3
        # second continue after finishing the first
        orch = _PipelineTargetTester(iteration=5, max_iterations=8)
        assert orch._get_max_iteration_target() - orch.iteration == 3
