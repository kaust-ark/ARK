"""LocalDb control-plane client must fail LOUDLY when its deps are broken.

ff5a2e5b: a teammate's `pip install otree` downgraded shared ark-base's
sqlalchemy to 1.3 → `website.dashboard.db` import failed → every sync no-oped
silently → an aborted run's 'failed' status was discarded and the exit-0
reaper marked it done. The client may degrade, but never silently.
"""

from unittest.mock import patch

from ark.controlplane.local_db import LocalDbControlPlaneClient


def _client_with_log():
    logs = []
    c = LocalDbControlPlaneClient(
        db_path="/nonexistent/webapp.db", project_id="p1",
        log_fn=lambda msg, level="INFO": logs.append((level, msg)))
    return c, logs


def test_broken_db_import_logs_error_and_unavailable():
    c, logs = _client_with_log()
    import builtins
    real_import = builtins.__import__

    def _no_dashboard(name, *a, **kw):
        if name.startswith("website"):
            raise ImportError("cannot import name 'create_mock_engine'")
        return real_import(name, *a, **kw)

    with patch("builtins.__import__", side_effect=_no_dashboard):
        assert c._db() is None
    assert c.available is False
    errors = [m for lvl, m in logs if lvl == "ERROR"]
    assert errors and "DISABLED" in errors[0]
    assert "NOT sync" in errors[0]  # states the consequence, not just the cause


def test_failure_logged_once_not_per_call():
    c, logs = _client_with_log()
    import builtins
    real_import = builtins.__import__

    def _no_dashboard(name, *a, **kw):
        if name.startswith("website"):
            raise ImportError("boom")
        return real_import(name, *a, **kw)

    with patch("builtins.__import__", side_effect=_no_dashboard):
        c._db()
        c._db()
        c._db()
    assert len([1 for lvl, _ in logs if lvl == "ERROR"]) == 1  # cached, not spammy
