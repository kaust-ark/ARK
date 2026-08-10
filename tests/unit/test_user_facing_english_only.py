"""Product surface is English-only.

The advisor messaged the assistant in English and got a hardcoded Chinese
acknowledgment back (2026-08). Development happens in Chinese; everything the
product EMITS — logs, notices, chat acks, Telegram, agent-visible prompts —
must be English.

This guard is deliberately narrow: it inspects string literals passed to
output-emitting calls only. Regexes that RECOGNISE Chinese user input (so
Chinese speakers are understood) are input, not output, and stay untouched.
"""

import ast
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

# Calls whose string arguments reach a human (dashboard, Telegram, run log,
# email) or an agent's prompt.
_EMITTERS = {
    "add_message", "send_telegram_notify", "send_notification", "notify_progress",
    "log", "log_step", "log_summary_box", "log_step_header",
    "send_admin_notice", "send_failure_email", "send_completion_email",
}

_SCANNED = [
    "website/dashboard/routes.py",
    "website/dashboard/sideband.py",
    "website/dashboard/app.py",
    "website/dashboard/notify.py",
    "website/dashboard/jobs.py",
    "ark/pipeline.py",
    "ark/orchestrator/core.py",
    "ark/engines/__init__.py",
]

_CJK = re.compile(r"[一-鿿]")


def _emitted_strings(tree: ast.AST):
    """Yield (lineno, text) for every string literal passed to an emitter."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name not in _EMITTERS:
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    yield sub.lineno, sub.value


@pytest.mark.parametrize("rel", _SCANNED)
def test_no_chinese_in_emitted_strings(rel):
    path = _REPO / rel
    if not path.exists():
        pytest.skip(f"{rel} not present")
    tree = ast.parse(path.read_text(errors="replace"))
    offenders = [(ln, txt[:60]) for ln, txt in _emitted_strings(tree) if _CJK.search(txt)]
    assert not offenders, (
        f"{rel} emits Chinese to the user — the product surface is English-only:\n"
        + "\n".join(f"  L{ln}: {txt}" for ln, txt in offenders)
    )


def test_guard_catches_a_planted_offender(tmp_path):
    """The guard must actually fire — a scanner that never trips is no guard."""
    src = tmp_path / "planted.py"
    src.write_text('add_message(session, pid, "agent", "收到，稍等")\n')
    tree = ast.parse(src.read_text())
    offenders = [t for _, t in _emitted_strings(tree) if _CJK.search(t)]
    assert offenders == ["收到，稍等"]


def test_input_matching_regexes_are_not_flagged(tmp_path):
    """Chinese in intent-classification patterns is INPUT handling — allowed."""
    src = tmp_path / "patterns.py"
    src.write_text('_ASK_RE = re.compile(r"进展|状态|到哪")\n')
    tree = ast.parse(src.read_text())
    assert list(_emitted_strings(tree)) == []
