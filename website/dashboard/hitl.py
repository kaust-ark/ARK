"""Control-plane HITL engine — the single owner of decision human-interaction.

Under D1 (see CONTROL_PLANE_BOUNDARY.md) the orchestrator no longer talks to
Telegram for decisions: it only opens a decision and polls for the answer. This
module is what the control plane runs instead — formatting + sending the outbound
notification, mapping an inbound reply onto the open decision, and enforcing
timeouts. It is pure/DB-driven with the Telegram transport injected, so the logic
is unit-testable without a bot token; the daemon wires the real sender in.
"""

from __future__ import annotations

import html as _html
import json
from typing import Callable, Optional

from .db import (
    PendingDecision,
    Session,
    answer_decision,
    get_open_decision,
    get_project,
    list_undelivered_decisions,
    mark_decision_notified,
    sweep_expired_decisions,
)


def _options(dec: PendingDecision) -> list[str]:
    try:
        return list(json.loads(dec.options or "[]"))
    except (ValueError, TypeError):
        return []


def parse_reply(reply: str, num_options: int) -> tuple[int, bool]:
    """(idx, is_freetext): a bare in-range number selects that option (1-based);
    anything else is free text. Mirrors the old orchestrator-side parser."""
    try:
        idx = int(str(reply).strip()) - 1
        if 0 <= idx < num_options:
            return idx, False
    except (ValueError, TypeError):
        pass
    return -1, True


def format_decision_message(dec: PendingDecision, project=None) -> str:
    """Render the rich HTML notification for a decision (moved off the
    orchestrator). Scannable: what happened, options with the default marked,
    and how to answer."""
    opts = _options(dec)
    name = getattr(project, "title", "") or getattr(project, "name", "") or "ARK"
    parts = [f"⚠️ <b>Decision needed</b> — {_html.escape(str(name))}"]
    if dec.question:
        parts += ["", _html.escape(dec.question)]
    if dec.context:
        parts += ["", "<b>Background</b>", _html.escape(dec.context)]
    if opts:
        parts += ["", "<b>Options</b>"]
        for i, opt in enumerate(opts):
            mark = "  ← default" if i == dec.default_index else ""
            parts.append(f"<b>{i + 1}.</b> {_html.escape(str(opt))}{mark}")
        parts += ["", f"Reply <b>1–{len(opts)}</b>, or type your own message."]
    return "\n".join(parts)


def apply_reply(session: Session, project_id: str, text: str, *,
                by: str = "telegram", source: str = "telegram") -> bool:
    """If the project has an open decision, record ``text`` as its answer and
    return True; else return False (caller treats it as a steer / chat)."""
    dec = get_open_decision(session, project_id)
    if dec is None:
        return False
    idx, is_text = parse_reply(text, len(_options(dec)))
    if is_text:
        answer_decision(session, dec.id, index=-1, text=text, by=by, source=source)
    else:
        answer_decision(session, dec.id, index=idx, text="", by=by, source=source)
    return True


# send_fn(bot_token, chat_id, text) -> bool
SendFn = Callable[[str, str, str], bool]


def deliver_pending(session: Session, send_fn: SendFn) -> int:
    """Send the outbound notification for every open decision not yet notified,
    then mark it notified (once, so the tick doesn't resend). Returns the count
    processed. A decision with no Telegram channel is still marked notified — the
    webapp shows it regardless."""
    n = 0
    for dec in list_undelivered_decisions(session):
        proj = get_project(session, dec.project_id)
        token = getattr(proj, "telegram_token", "") if proj else ""
        chat = getattr(proj, "telegram_chat_id", "") if proj else ""
        if token and chat:
            try:
                send_fn(token, chat, format_decision_message(dec, proj))
            except Exception:
                pass
        mark_decision_notified(session, dec.id)
        n += 1
    return n


def sweep(session: Session) -> list[str]:
    """Expire decisions past their deadline (control plane owns timeouts)."""
    return sweep_expired_decisions(session)
