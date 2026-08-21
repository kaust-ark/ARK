"""Email-path hardening: what a self-hosted CLI run may put in other people's
inboxes.

Two regressions guarded here, both observed live on 2026-08-19/20 from a
teammate's pre-v0.5.21 install:

1. The mail fallback piped Telegram HTML into `mail`, so recipients saw
   literal "<b>🏁 ══ FINISHED ══</b>" markup.
2. The end-of-run summary notified on EVERY invocation, including reruns
   that performed zero new iterations — under an external rerun wrapper
   that is one email per invocation, forever.
"""

import subprocess
from unittest import mock

import pytest

from ark.orchestrator.core import Orchestrator
from ark.pipeline import PipelineMixin


def _bare_orchestrator(config):
    """Orchestrator with only what send_notification touches."""
    o = Orchestrator.__new__(Orchestrator)
    o.config = config
    o.project_name = "demo"
    o.log = lambda *a, **k: None
    o.telegram = mock.Mock(is_configured=False)
    return o


class TestMailBodyIsPlainText:
    def test_html_tags_stripped_from_email(self):
        o = _bare_orchestrator({"notification_email": "someone@example.com"})
        with mock.patch.object(subprocess, "run") as run:
            o.send_notification("DEMO Finished", "Score: 8.6/10", priority="critical")
        assert run.called
        body = run.call_args.kwargs.get("input") or run.call_args.args[1]
        assert "<b>" not in body and "</b>" not in body
        assert "FINISHED" in body            # banner text survives
        assert "Score: 8.6/10" in body       # payload survives

    def test_opt_in_gate_still_holds(self):
        o = _bare_orchestrator({})           # no notification_email
        with mock.patch.object(subprocess, "run") as run:
            o.send_notification("DEMO Finished", "Score: 8.6/10", priority="critical")
        run.assert_not_called()


class TestEndSummaryOnlyWhenNewWorkHappened:
    def test_zero_iteration_rerun_is_silent(self):
        assert PipelineMixin._should_send_end_summary("in_progress", 0) is False

    def test_run_with_new_iterations_notifies(self):
        assert PipelineMixin._should_send_end_summary("in_progress", 3) is True

    def test_accepted_never_double_notifies(self):
        # ACCEPTED already sent its own notice inside the iteration.
        assert PipelineMixin._should_send_end_summary("accepted", 2) is False

    def test_failed_run_with_work_still_notifies(self):
        assert PipelineMixin._should_send_end_summary("failed", 1) is True
