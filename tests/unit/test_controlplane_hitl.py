"""Control-plane HITL engine (Phase 1, step 6 / D1).

Tests the DB-driven decision fan-out that replaces the orchestrator's Telegram
handling: inbound reply → answer, outbound notify (injected sender), and the
timeout sweep. No bot token required.
"""

from datetime import datetime, timedelta

import pytest


@pytest.fixture
def db_project(tmp_path, monkeypatch):
    import website.dashboard.db as db
    monkeypatch.setattr(db, "_engine", None, raising=False)
    db_path = str(tmp_path / "webapp.db")
    with db.get_session(db_path) as s:
        user, _ = db.get_or_create_user_by_email(s, "h@example.com")
        project = db.create_project(s, user_id=user.id, name="hitl-proj",
                                    telegram_token="tok", telegram_chat_id="chat")
        pid = project.id
    return db, db_path, pid


# ── parsing / formatting ────────────────────────────────────────────────────────

def test_parse_reply_number_vs_freetext():
    from website.dashboard.hitl import parse_reply
    assert parse_reply("2", 3) == (1, False)     # 1-based → index 1
    assert parse_reply("9", 3) == (-1, True)     # out of range → free text
    assert parse_reply("use pytorch", 3) == (-1, True)


def test_format_decision_message_has_question_and_options(db_project):
    db, db_path, pid = db_project
    from website.dashboard.hitl import format_decision_message
    with db.get_session(db_path) as s:
        did = db.create_pending_decision(s, pid, "Proceed?", ["Yes", "No"],
                                         context="ran out of memory", default_index=1)
        dec = db.get_decision(s, did)
        msg = format_decision_message(dec)
    assert "Proceed?" in msg
    assert "Yes" in msg and "No" in msg
    assert "← default" in msg          # marks option #2 (index 1)
    assert "ran out of memory" in msg


# ── inbound reply → answer ───────────────────────────────────────────────────────

def test_apply_reply_numeric(db_project):
    db, db_path, pid = db_project
    from website.dashboard.hitl import apply_reply
    with db.get_session(db_path) as s:
        did = db.create_pending_decision(s, pid, "Proceed?", ["Yes", "No"])
    with db.get_session(db_path) as s:
        assert apply_reply(s, pid, "1") is True
    with db.get_session(db_path) as s:
        dec = db.get_decision(s, did)
    assert dec.status == "answered" and dec.answer_index == 0 and dec.source == "telegram"


def test_apply_reply_freetext(db_project):
    db, db_path, pid = db_project
    from website.dashboard.hitl import apply_reply
    with db.get_session(db_path) as s:
        did = db.create_pending_decision(s, pid, "Proceed?", ["Yes", "No"])
    with db.get_session(db_path) as s:
        assert apply_reply(s, pid, "actually, try a smaller batch") is True
    with db.get_session(db_path) as s:
        dec = db.get_decision(s, did)
    assert dec.status == "answered" and dec.answer_index == -1
    assert dec.answer_text == "actually, try a smaller batch"


def test_apply_reply_no_open_decision_returns_false(db_project):
    db, db_path, pid = db_project
    from website.dashboard.hitl import apply_reply
    with db.get_session(db_path) as s:
        assert apply_reply(s, pid, "anything") is False


# ── outbound notify (injected sender) ────────────────────────────────────────────

def test_deliver_pending_sends_once(db_project):
    db, db_path, pid = db_project
    from website.dashboard.hitl import deliver_pending
    with db.get_session(db_path) as s:
        db.create_pending_decision(s, pid, "Proceed?", ["Yes", "No"])

    sent = []
    send_fn = lambda tok, chat, text: sent.append((tok, chat, text)) or True

    with db.get_session(db_path) as s:
        assert deliver_pending(s, send_fn) == 1
    assert len(sent) == 1
    assert sent[0][0] == "tok" and sent[0][1] == "chat"
    assert "Proceed?" in sent[0][2]

    # already notified → not resent
    with db.get_session(db_path) as s:
        assert deliver_pending(s, send_fn) == 0
    assert len(sent) == 1


def test_deliver_pending_no_channel_marks_notified(tmp_path, monkeypatch):
    import website.dashboard.db as db
    monkeypatch.setattr(db, "_engine", None, raising=False)
    db_path = str(tmp_path / "webapp.db")
    with db.get_session(db_path) as s:
        u, _ = db.get_or_create_user_by_email(s, "n@example.com")
        p = db.create_project(s, user_id=u.id, name="no-tg")  # no telegram fields
        pid = p.id
        db.create_pending_decision(s, pid, "Q", ["a", "b"])
    from website.dashboard.hitl import deliver_pending
    sent = []
    with db.get_session(db_path) as s:
        assert deliver_pending(s, lambda *a: sent.append(a) or True) == 1
    assert sent == []  # no channel → nothing sent, but still marked notified
    with db.get_session(db_path) as s:
        assert deliver_pending(s, lambda *a: sent.append(a) or True) == 0


# ── timeout sweep ─────────────────────────────────────────────────────────────────

def test_sweep_expires_past_deadline_only(db_project):
    db, db_path, pid = db_project
    from website.dashboard.hitl import sweep
    past = datetime.utcnow() - timedelta(minutes=5)
    future = datetime.utcnow() + timedelta(minutes=5)
    with db.get_session(db_path) as s:
        d_past = db.create_pending_decision(s, pid, "old", ["a"], deadline_at=past)
    # create_pending_decision cancels prior open decisions for a project, so put
    # the future one in a second project to keep both pending.
    with db.get_session(db_path) as s:
        u = db.get_or_create_user_by_email(s, "h@example.com")[0]
        p2 = db.create_project(s, user_id=u.id, name="p2")
        d_future = db.create_pending_decision(s, p2.id, "new", ["a"], deadline_at=future)

    with db.get_session(db_path) as s:
        swept = sweep(s)
    assert swept == [d_past]
    with db.get_session(db_path) as s:
        assert db.get_decision(s, d_past).status == "timed_out"
        assert db.get_decision(s, d_future).status == "pending"
