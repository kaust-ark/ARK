"""The orchestrator's email fallback must use a transport that exists.

Telegram carries notifications on a healthy deployment, so the email branch
only runs when Telegram is unconfigured or already failing — which is exactly
when the message matters (STAGNATION, FINISHED, quota). That branch shelled
out to a `mail` binary absent from our hosts, so it died on ENOENT every time
and the notification was lost while the log said only "Failed to send".
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ark.orchestrator.core import Orchestrator


@pytest.fixture
def orch():
    o = Orchestrator.__new__(Orchestrator)
    o.project_name = "proj"
    o.config = {}
    o.logs = []
    o.log = lambda msg, level="INFO": o.logs.append((level, msg))
    o.telegram = SimpleNamespace(is_configured=False, send=MagicMock())
    return o


def _patch_mailer(sent, ok=True):
    """Stand in for the dashboard mailer, recording what it was handed."""
    def _send(settings, to, subject, body):
        sent.append({"to": to, "subject": subject, "body": body})
        return ok
    return patch.dict("sys.modules", {
        "website.dashboard.config": SimpleNamespace(
            get_settings=lambda: SimpleNamespace(admin_emails=["ops@example.org"])),
        "website.dashboard.notify": SimpleNamespace(send_admin_notice=_send),
    })


def test_email_fallback_goes_through_the_smtp_mailer(orch):
    sent = []
    # priority="critical" is how a non-keyword subject reaches the send path at
    # all; anything else is routed to a progress ping and never emailed.
    with _patch_mailer(sent), patch("subprocess.run") as run:
        orch.send_notification("Stagnation detected", "no progress in 3 rounds",
                               priority="critical")
    assert len(sent) == 1
    assert not run.called, "must not shell out to a `mail` binary"
    assert "PROJ" in sent[0]["subject"] and "Stagnation" in sent[0]["subject"]
    assert any(lvl == "INFO" and "sent" in m for lvl, m in orch.logs)


def test_html_markup_does_not_leak_into_the_email_body(orch):
    """The banner is Telegram HTML; an email reader would show the tags."""
    sent = []
    with _patch_mailer(sent):
        orch.send_notification("Run finished", "all good")
    body = sent[0]["body"]
    assert "<b>" not in body and "</b>" not in body
    assert "FINISHED" in body and "all good" in body


def test_an_explicit_notification_email_wins_over_the_admin_default(orch):
    sent = []
    orch.config = {"notification_email": "owner@example.org"}
    with _patch_mailer(sent):
        orch.send_notification("Run finished", "done")
    assert sent[0]["to"] == "owner@example.org"


def test_a_transport_that_refuses_is_reported_as_not_sent(orch):
    """False from the mailer is a real delivery failure, not a success."""
    sent = []
    with _patch_mailer(sent, ok=False):
        orch.send_notification("Rate limit hit", "backing off", priority="critical")
    assert any(lvl == "WARN" and "NOT sent" in m for lvl, m in orch.logs)
    assert not any("Email notification sent" in m for _, m in orch.logs)


def test_telegram_still_takes_precedence_and_skips_email(orch):
    sent = []
    orch.telegram = SimpleNamespace(is_configured=True, send=MagicMock())
    with _patch_mailer(sent):
        orch.send_notification("Run finished", "done")
    assert orch.telegram.send.called
    assert sent == []


def test_a_standalone_ark_install_degrades_quietly(orch):
    """ark is pip-installable without the dashboard; a missing import must
    warn, not raise into the caller's control flow."""
    with patch.dict("sys.modules", {"website.dashboard.config": None,
                                    "website.dashboard.notify": None}):
        orch.send_notification("Run finished", "done")   # must not raise
    assert any(lvl == "WARN" for lvl, _ in orch.logs)


def test_quiet_mode_silences_every_channel(orch):
    """Dev/smoke runs must not page anyone: a night of pipeline testing
    emailed the operator on every transient error. The log keeps the record."""
    orch.config = {"suppress_notifications": True}
    orch.telegram = SimpleNamespace(is_configured=True, send=MagicMock())
    sent = []
    with _patch_mailer(sent):
        orch.send_notification("Run finished", "done", priority="critical")
    assert not orch.telegram.send.called
    assert sent == []
    assert any("suppressed" in m for _, m in orch.logs)
