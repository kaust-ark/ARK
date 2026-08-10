"""A wedged run must give up, not sit at "running" forever.

2026-08-03: a conda clone wedged; the 60-minute alert fired into the void
(the owner had no Telegram configured — token and chat_id both empty) and the
project stayed "running" for five days, holding a queue lane. Alerting alone
is not a safety net; the watchdog has to terminate the run so it reaches
people through the ordinary failed-run notification path.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from website.dashboard import app as dash


@pytest.fixture(autouse=True)
def _clean_state():
    dash._log_mtimes.clear()
    dash._stuck_alerted.clear()
    yield
    dash._log_mtimes.clear()
    dash._stuck_alerted.clear()


def _project(pid="p1"):
    return SimpleNamespace(id=pid, status="running", title="T", name="t",
                           slurm_job_id="local:123", telegram_token="",
                           telegram_chat_id="", user_id="u1")


def _launcher(mtime):
    lz = MagicMock()
    lz.latest_log_mtime.return_value = mtime
    return lz


def _idle(minutes):
    import time
    return time.time() - minutes * 60


def test_wedged_run_is_cancelled_and_failed(tmp_path):
    p, lz, session = _project(), _launcher(_idle(200)), MagicMock()
    with patch("website.dashboard.db.update_project") as _upd:
        dash._stuck_watchdog(p, lz, tmp_path, session)

    lz.cancel.assert_called_once()          # the job is actually stopped
    _upd.assert_called_once()
    kw = _upd.call_args.kwargs
    assert kw["status"] == "failed"
    # The message must tell a human what happened and what to do.
    assert "stuck" in kw["error_message"]
    assert "restart or continue" in kw["error_message"].lower()


def test_merely_slow_run_is_alerted_not_killed(tmp_path):
    p, lz, session = _project(), _launcher(_idle(90)), MagicMock()
    with patch.object(dash, "send_telegram_notify") as tg, \
         patch("website.dashboard.db.update_project") as upd:
        dash._stuck_watchdog(p, lz, tmp_path, session)

    tg.assert_called_once()                 # warn at 60 min
    upd.assert_not_called()                 # but do NOT kill at 90 min


def test_progress_clears_a_prior_alert(tmp_path):
    p, session = _project(), MagicMock()
    with patch.object(dash, "send_telegram_notify"):
        dash._stuck_watchdog(p, _launcher(_idle(90)), tmp_path, session)
    assert p.id in dash._stuck_alerted

    # New output appears → the project is alive again.
    with patch("website.dashboard.db.update_project") as upd:
        dash._stuck_watchdog(p, _launcher(_idle(0)), tmp_path, session)
    assert p.id not in dash._stuck_alerted
    upd.assert_not_called()


def test_only_running_projects_are_watched(tmp_path):
    p = _project()
    p.status = "done"
    with patch("website.dashboard.db.update_project") as upd:
        dash._stuck_watchdog(p, _launcher(_idle(500)), tmp_path, MagicMock())
    upd.assert_not_called()


def test_cancel_failure_does_not_block_the_fail_transition(tmp_path):
    """A backend that cannot be cancelled must still not stay 'running'."""
    p, session = _project(), MagicMock()
    lz = _launcher(_idle(200))
    lz.cancel.side_effect = RuntimeError("backend gone")
    with patch("website.dashboard.db.update_project") as _upd:
        dash._stuck_watchdog(p, lz, tmp_path, session)
    assert _upd.call_args.kwargs["status"] == "failed"
