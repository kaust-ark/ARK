"""A finished experiment always gets a write-up task.

Writer tasks for experiments used to come only from `issue.actions[]` entries
with `agent: writer`. The planner's output schema has no `actions` key, and in
12 production action plans 0 of 143 issues carried one, so experiments run in
the review loop never reached the paper.
"""

from unittest.mock import MagicMock

from ark.execution import ExecutionMixin


def _collect(issues):
    s = MagicMock(spec=ExecutionMixin)
    s.log = MagicMock()
    return ExecutionMixin._collect_writing_tasks(s, issues)


def test_a_completed_experiment_without_actions_gets_a_writeup():
    tasks = _collect([{"id": "M2", "type": "EXPERIMENT_REQUIRED", "status": "completed",
                       "title": "Run the EnKF baseline", "description": "Add EnKF to Table 2"}])
    assert len(tasks) == 1
    t = tasks[0]
    assert t["_from_experiment"] and t["id"] == "M2"
    assert "results/" in t["description"] and "Add EnKF to Table 2" in t["description"]


def test_a_failed_experiment_gets_an_honesty_task_not_numbers():
    tasks = _collect([{"id": "M3", "type": "EXPERIMENT_REQUIRED", "status": "failed",
                       "title": "Ablation", "description": "ablate the router"}])
    assert len(tasks) == 1
    assert "Do not fill in numbers" in tasks[0]["description"]


def test_planner_supplied_writer_actions_are_kept():
    action = {"agent": "writer", "task": "Rewrite Section 5.2 with the new CIs"}
    tasks = _collect([{"id": "M4", "type": "EXPERIMENT_REQUIRED", "status": "completed",
                       "title": "CIs", "actions": [action, {"agent": "coder", "task": "x"}]}])
    assert [t["description"] for t in tasks] == ["Rewrite Section 5.2 with the new CIs"]


def test_pending_experiments_produce_no_writeup():
    assert _collect([{"id": "M5", "type": "EXPERIMENT_REQUIRED", "status": "pending",
                      "title": "later"}]) == []


def test_a_completed_literature_issue_gets_a_writeup():
    tasks = _collect([{"id": "m1", "type": "LITERATURE_REQUIRED", "status": "completed",
                       "title": "Related work", "description": "cover KV-cache work"}])
    assert len(tasks) == 1 and "cover KV-cache work" in tasks[0]["description"]


def test_writing_only_issues_are_unchanged():
    issue = {"id": "m2", "type": "WRITING_ONLY", "title": "Typo", "description": "fix"}
    tasks = _collect([issue])
    assert tasks == [{"id": "m2", "title": "Typo", "description": "fix", "actions": []}]
    assert issue["status"] == "in_progress"
