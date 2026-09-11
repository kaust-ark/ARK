"""The control plane's decisions must be visible in the service log.

2026-09-10: a healthy run kept being marked failed. Every branch that could
have done it logs at INFO — and NONE of those lines existed in the journal,
because nothing ever gave our loggers a handler. uvicorn's log_level only
configures uvicorn's own loggers, so `logger.info()` in website.dashboard fell
through to logging.lastResort, which emits WARNING and above. Hours went into
reconstructing from the outside what one log line would have said.
"""

import logging

import pytest

from ark import cli


@pytest.fixture(autouse=True)
def _fresh_loggers():
    """Save and restore the loggers this module reconfigures.

    Two reasons. The handler binds whatever sys.stdout was current when it was
    installed — right for the service (one stable stream per process), wrong
    under capsys, which swaps stdout per test. And _configure_app_logging sets
    propagate=False, which silently breaks any LATER test that reads ark.*
    records through caplog if we leak it (it broke test_skypilot_launcher
    exactly once, on the first run of this file).
    """
    names = ("website.dashboard", "ark")
    saved = {n: (list(logging.getLogger(n).handlers),
                 logging.getLogger(n).propagate,
                 logging.getLogger(n).level) for n in names}
    yield
    for n, (handlers, propagate, level) in saved.items():
        lg = logging.getLogger(n)
        lg.handlers = handlers
        lg.propagate = propagate
        lg.setLevel(level)


class TestOurLoggersReachTheServiceLog:
    def test_dashboard_logger_emits_info(self, capsys):
        cli._configure_app_logging()
        logging.getLogger("website.dashboard").info("marked failed")
        assert "marked failed" in capsys.readouterr().out

    def test_ark_logger_emits_info(self, capsys):
        cli._configure_app_logging()
        logging.getLogger("ark").info("launcher says running")
        assert "launcher says running" in capsys.readouterr().out

    def test_third_party_info_stays_out(self, capsys):
        """Scoped on purpose — httpx/litellm INFO would bury the signal."""
        cli._configure_app_logging()
        logging.getLogger("httpx").info("GET /v1/models 200")
        assert "GET /v1/models" not in capsys.readouterr().out

    def test_repeated_setup_does_not_duplicate_lines(self, capsys):
        """create_app can run more than once in a process (tests, reloads)."""
        cli._configure_app_logging()
        cli._configure_app_logging()
        logging.getLogger("website.dashboard").info("once")
        assert capsys.readouterr().out.count("once") == 1
