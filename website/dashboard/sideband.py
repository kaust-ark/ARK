"""Sideband chat router for a running (or finished) project.

A user's chat message is one of three things; the webapp must decide which
BEFORE touching the orchestrator:

  - DECISION answer  → handled by the caller when a decision is open
  - ASK   (status/state question, "现在咋样了") → answered here, instantly, from
            the project's DB state read-model. It never reaches the running agent.
  - STEER (an instruction, "把图做大点") → enqueued for the orchestrator + acked.

This split fixes the original bug where a status question was treated as a
steer and injected into the agent's working context (user_updates.yaml),
polluting it and replying with a meaningless "Got it — I'll apply that".

Everything here is fail-soft: if no model key is available or the LLM call
fails, ASK falls back to a deterministic state summary and classify falls back
to a question-mark heuristic, so the user always gets *something*.
"""
from __future__ import annotations

import logging
import re

from .db import list_messages

logger = logging.getLogger(__name__)

# Heuristic fast-paths (free, instant). Short-circuit the LLM for obvious cases
# and act as the fallback when no key / the LLM call fails.
_ASK_HINTS = re.compile(
    r"[?？]|^(what|how|why|when|where|which|who|is |are |did |does |can you tell|status|progress)"
    r"|怎么|咋|啥|多少|进展|到哪|哪一?步|状态|为什么|为啥|是不是|有没有|完了吗|好了吗|过了吗|现在",
    re.I,
)
_STEER_HINTS = re.compile(
    r"^(please |pls |make |change |use |add |remove |focus|don'?t|do not|stop using|switch|set |avoid|prefer|should use|instead)"
    r"|把|改成?|别|不要|换成|聚焦|加上|去掉|删掉|应该用|改用|换个|重新|重写|重做|扩写|缩短|补充|换成",
    re.I,
)


def _heuristic_intent(text: str) -> str | None:
    t = (text or "").strip()
    steer = bool(_STEER_HINTS.search(t))
    ask = bool(_ASK_HINTS.search(t))
    if ask and not steer:
        return "ask"
    if steer and not ask:
        return "steer"
    return None  # ambiguous → let the LLM (or the default) decide


def _cheap_model_and_auth(keys: dict):
    """(litellm_model, api_key) for the cheapest available provider, or (None, None)."""
    keys = keys or {}
    if keys.get("anthropic"):
        return "anthropic/claude-haiku-4-5", keys["anthropic"]
    if keys.get("openai"):
        return "openai/gpt-5.4-mini", keys["openai"]
    if keys.get("gemini"):
        return "gemini/gemini-2.5-flash", keys["gemini"]
    if keys.get("openrouter"):
        return "openrouter/anthropic/claude-haiku-4.5", keys["openrouter"]
    return None, None


def _cheap_llm(keys: dict, system: str, user: str, max_tokens: int = 500,
               timeout: int = 15) -> str | None:
    model, api_key = _cheap_model_and_auth(keys)
    if not model:
        return None
    try:
        import litellm
        resp = litellm.completion(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            api_key=api_key, max_tokens=max_tokens, temperature=0.2, timeout=timeout,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning(f"sideband cheap LLM failed: {e}")
        return None


def build_state_readmodel(session, project, max_events: int = 14) -> str:
    """A compact text snapshot of where the project is, from the DB only.
    Call this while a DB session is open; the result is a plain string that can
    then be handed to a thread-offloaded LLM call."""
    lines = [
        f"Status: {getattr(project, 'status', '?')}",
        f"Current activity: {getattr(project, 'activity', '') or '—'}",
    ]
    title = getattr(project, "title", "") or ""
    if title:
        lines.append(f"Title: {title}")
    try:
        msgs = list_messages(session, project.id)
    except Exception:
        msgs = []
    interesting = [m for m in msgs
                   if m.kind in ("milestone", "notice", "message", "decision")]
    recent = interesting[-max_events:]
    if recent:
        lines.append("Recent progress (oldest→newest):")
        for m in recent:
            txt = (m.text or "").strip().replace("\n", " ")
            if len(txt) > 200:
                txt = txt[:200] + "…"
            lines.append(f"  [{m.role}/{m.kind[:4]}] {txt}")
    return "\n".join(lines)


def classify_message(text: str, keys: dict) -> str:
    """Return 'ask' or 'steer'. Heuristic fast-path, cheap-LLM tiebreak,
    question-mark default. Never raises."""
    h = _heuristic_intent(text)
    if h:
        return h
    system = (
        "Classify a user's chat message to their AI research project as exactly "
        "one word:\n"
        "  'ask'   — they want information: status, progress, results, why a "
        "choice was made, what a figure/number means, how it's going. Questions.\n"
        "  'steer' — a clear, actionable INSTRUCTION to change the work: add/"
        "remove/redo something, use a different method, fix X, rewrite Y.\n"
        "When in doubt, choose 'ask' — only answer 'steer' for an unmistakable "
        "command to change something. Reply with ONLY 'ask' or 'steer'."
    )
    out = (_cheap_llm(keys, system, text or "", max_tokens=4, timeout=10) or "").lower()
    if "steer" in out:
        return "steer"
    if "ask" in out:
        return "ask"
    # Ambiguous + no LLM: a question mark leans ask; otherwise default to ask,
    # which is the *safe* default — answering never pollutes the agent context,
    # whereas a misrouted steer would.
    return "ask"


_EXPERIMENT_HINTS = re.compile(
    r"\b(experiment|baseline|ablation|benchmark|evaluate|measure|re-?run|run the|run a|dataset)\b"
    r"|实验|基线|消融|评测|跑(一|个|实验)|重新跑|数据集|复现|对比实验",
    re.I,
)
_FULL_HINTS = re.compile(
    r"\b(overall|improve everything|address (all|the) (review|reviewer)|make it better|polish the (whole|entire)|another iteration|full iteration)\b"
    r"|整体|全面|从头|大改|所有(问题|意见)|再迭代|完整迭代|重写整篇",
    re.I,
)


def classify_steer_scope(text: str, keys: dict) -> str:
    """For a STEER, pick the smallest sufficient mechanism:
      'edit'       — local prose/LaTeX/figure change → one agent, no loop.
      'experiment' — needs real compute (run/add an experiment).
      'full'       — broad "make it better overall" → a full iteration.
    Heuristic first, cheap-LLM tiebreak, default 'edit' (the cheapest)."""
    t = text or ""
    if _FULL_HINTS.search(t):
        return "full"
    if _EXPERIMENT_HINTS.search(t):
        return "experiment"
    system = (
        "A user gave an INSTRUCTION to change their finished research paper. Pick the "
        "smallest mechanism, one word:\n"
        "  'edit'       — a local change to text/LaTeX/a figure (rewrite, shorten, fix, "
        "relabel, resize). One quick agent pass.\n"
        "  'experiment' — requires running/adding a real experiment or new results.\n"
        "  'full'       — a broad 'improve the whole paper / address all reviews' request.\n"
        "Default to 'edit' unless it clearly needs an experiment or a full overhaul. "
        "Reply with ONLY 'edit', 'experiment', or 'full'."
    )
    out = (_cheap_llm(keys, system, t, max_tokens=4, timeout=10) or "").lower()
    for s in ("experiment", "full", "edit"):
        if s in out:
            return s
    return "edit"


def answer_from_state(question: str, state_text: str, keys: dict) -> str:
    """Answer a status question from the prebuilt state snapshot. Deterministic
    fallback (the raw snapshot) if no LLM key / the call fails."""
    system = (
        "You are the progress reporter for a user's running paper-writing project "
        "on an AI research platform. Answer the user's question concisely and "
        "concretely, USING ONLY the state snapshot below — never invent steps, "
        "numbers, or results. Reply in the user's language (Chinese question → "
        "Chinese answer). 2–5 sentences. You report progress; you do NOT steer the agent."
    )
    user = f"State snapshot:\n{state_text}\n\nUser question: {question}\n\nAnswer:"
    out = _cheap_llm(keys, system, user, max_tokens=500, timeout=15)
    if out:
        return out
    return "📊 当前进度:\n" + state_text
