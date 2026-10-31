"""A login link signs in whoever holds it, so it must not outlive its email.

Every magic link was printed to the service journal whether or not the email
went out, uvicorn logged the same token again on the /auth/verify request
line, and the token was accepted for 30 days (the email says 1 hour). Any
reader of the journal could sign in as any user who logged in that month.
"""
import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.requests import Request

from ark import cli
from website.dashboard import auth
from website.dashboard import routes as R


def _send_link(email: str, delivered: bool, capsys) -> str:
    body = json.dumps({"email": email}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    req = Request({"type": "http", "method": "POST", "path": "/auth/send-link",
                   "headers": [(b"content-type", b"application/json")],
                   "client": ("10.0.0.9", 0), "query_string": b""}, receive)
    settings = SimpleNamespace(allowed_emails=[], email_domains=[], secret_key="s",
                               base_url="https://example.test")
    R._SENDLINK_BY_IP.clear(); R._SENDLINK_BY_EMAIL.clear(); R._SENDLINK_GLOBAL.clear()
    with patch.object(R, "get_settings", return_value=settings), \
         patch.object(R, "send_magic_link_email", return_value=delivered):
        asyncio.run(R.auth_send_link(req))
    return capsys.readouterr().out


def test_a_delivered_link_is_not_printed(capsys):
    out = _send_link("a@example.test", delivered=True, capsys=capsys)
    assert "token=" not in out and "MAGIC LINK" not in out


def test_an_undelivered_link_is_still_printed_for_the_operator(capsys):
    out = _send_link("b@example.test", delivered=False, capsys=capsys)
    assert "MAGIC LINK for b@example.test" in out and "token=" in out


def test_the_link_expires_in_the_hour_the_email_promises():
    tok = auth.make_token("c@example.test", "s")
    assert auth.verify_token(tok, "s") == "c@example.test"
    with patch("itsdangerous.timed.time.time", return_value=__import__("time").time() + 3601):
        assert auth.verify_token(tok, "s") is None


@pytest.fixture
def logging_restored():
    """_configure_app_logging and uvicorn's dictConfig rewire real loggers; put
    them back so later tests' caplog still sees website.dashboard records."""
    names = ("website.dashboard", "ark", "uvicorn", "uvicorn.access", "uvicorn.error")
    saved = {n: (lg.handlers[:], lg.filters[:], lg.level, lg.propagate, lg.disabled)
             for n in names for lg in [logging.getLogger(n)]}
    yield
    for n, (h, f, lvl, prop, dis) in saved.items():
        lg = logging.getLogger(n)
        lg.handlers[:], lg.filters[:] = h, f
        lg.setLevel(lvl); lg.propagate = prop; lg.disabled = dis


def test_the_access_log_never_carries_a_token(logging_restored):
    """The filter must survive the dictConfig uvicorn runs after our setup."""
    from uvicorn.config import Config

    cli._configure_app_logging()
    cli._configure_app_logging()  # idempotent: one filter, not two
    Config(app=lambda *a: None, log_level="info")
    lg = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, cli._RedactLoginTokens) for f in lg.filters) == 1

    rec = lg.makeRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                        ("1.2.3.4:0", "GET", "/dashboard/auth/verify?token=abc.DEF-1_x&n=2",
                         "1.1", 307), None)
    # Only ours: newer litellm attaches its own scrubber to uvicorn.access on
    # import, which rewrites the same span differently. Prod must not rely on it.
    ours = next(f for f in lg.filters if isinstance(f, cli._RedactLoginTokens))
    ours.filter(rec)
    line = rec.getMessage()
    assert "abc.DEF-1_x" not in line
    assert "token=[redacted]&n=2" in line and line.endswith("307")


def test_other_request_lines_are_left_alone():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:0", "GET", "/dashboard/api/projects?page=2", "1.1", 200), None)
    cli._RedactLoginTokens().filter(rec)
    assert rec.args[2] == "/dashboard/api/projects?page=2"
