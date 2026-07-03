"""Persistent, streaming OpenHands chat agent for out-of-band project management.

This is the Claude-Code-level chat backend: instead of classifying each message
and spawning a fresh one-shot agent, we keep ONE OpenHands conversation per
project and resume it for every message (``openhands --resume <id> -t <msg>``),
so the agent has full conversational memory + file access and decides for itself
whether to answer, read, edit, or run. Its tool steps stream to the chat live.

Validated: OpenHands ``--resume`` preserves conversation memory across turns.

Used by the orchestrator's chat mode (see core.main --chat-message). Kept as a
plain function so it can be unit-tested directly against a project directory.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

from ark.observability.steps import parse_line

# conversation id is stored here, inside the project workspace
CONV_FILE = ".ark_chat_conversation"


def load_conversation_id(workspace: Path) -> Optional[str]:
    f = Path(workspace) / CONV_FILE
    try:
        return f.read_text().strip() or None
    except OSError:
        return None


def save_conversation_id(workspace: Path, conv_id: str) -> None:
    try:
        (Path(workspace) / CONV_FILE).write_text(conv_id.strip())
    except OSError:
        pass


def read_conversation_usage(workspace, conversation_id: Optional[str]) -> Optional[dict]:
    """CUMULATIVE token/cost totals of the persistent chat conversation.

    OpenHands doesn't stream cost; it persists it to
    ``<HOME>/.openhands/conversations/<id>/base_state.json`` — and this runner
    sets ``HOME=workspace``, so the file lives inside the project. Mirrors
    ``OpenHandsCLI._read_usage``. Cumulative across resumed turns — callers
    diff against their last recorded total to get a per-turn delta.
    """
    if not conversation_id:
        return None
    try:
        bs = (Path(workspace) / ".openhands" / "conversations"
              / conversation_id / "base_state.json")
        if not bs.exists():
            return None
        data = json.loads(bs.read_text())
        metrics = (data.get("stats") or {}).get("usage_to_metrics") or {}
        cost = 0.0
        in_tok = out_tok = cache_read = cache_write = 0
        for m in metrics.values():
            cost += float(m.get("accumulated_cost") or 0.0)
            tu = m.get("accumulated_token_usage") or {}
            in_tok += int(tu.get("prompt_tokens") or 0)
            out_tok += int(tu.get("completion_tokens") or 0)
            cache_read += int(tu.get("cache_read_tokens") or 0)
            cache_write += int(tu.get("cache_write_tokens") or 0)
        return {
            "cost_usd": cost,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_write,
        }
    except Exception:
        return None


def _build_env(model: str) -> dict:
    """OpenHands env: LLM_MODEL + the provider's key (same convention as the
    OpenHandsCLI engine), with the conversation persisted under workspace HOME."""
    from ark.llm_lite import provider_key_env
    env = os.environ.copy()
    env["LLM_MODEL"] = model
    provider = model.split("/", 1)[0] if "/" in model else ""
    key = os.environ.get(provider_key_env(provider)) if provider else None
    if key:
        env["LLM_API_KEY"] = key
    if os.environ.get("LLM_BASE_URL"):
        env["LLM_BASE_URL"] = os.environ["LLM_BASE_URL"]
    env["OPENHANDS_SUPPRESS_BANNER"] = "1"
    return env


def run_chat_turn(*, workspace, message: str, model: str,
                  conversation_id: Optional[str] = None,
                  on_step: Optional[Callable] = None,
                  on_answer: Optional[Callable[[str], None]] = None,
                  log: Callable = print,
                  timeout: int = 1800) -> tuple[str, Optional[str], bool]:
    """Run ONE chat turn in ``workspace`` via a persistent OpenHands conversation.

    Resumes ``conversation_id`` if given (full memory); otherwise starts a new
    conversation. Streams tool steps via ``on_step(StepEvent)`` as they happen and
    delivers the final agent message via ``on_answer(text)``. Returns
    ``(answer, conversation_id, ok)``."""
    workspace = Path(workspace)
    env = _build_env(model)
    env["HOME"] = str(workspace)  # conversations persist under workspace/.openhands

    cmd = ["openhands", "--headless", "--json", "--override-with-envs"]
    if conversation_id:
        cmd += ["--resume", conversation_id]
    cmd += ["-t", message]

    log(f"[chat] openhands {'resume '+conversation_id if conversation_id else 'new'} "
        f"model={model} ws={workspace}")
    proc = subprocess.Popen(
        cmd, cwd=str(workspace), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, text=True, bufsize=1,
    )
    conv_id = conversation_id
    last_agent_msg = ""
    finish_msg = ""
    try:
        for line in proc.stdout:
            s = line.strip()
            if conv_id is None and s.startswith("Conversation ID:"):
                conv_id = s.split(":", 1)[1].strip()
                continue
            if s.startswith("{") and s.endswith("}"):
                try:
                    evt = json.loads(s)
                except (json.JSONDecodeError, ValueError):
                    evt = None
                if evt:
                    kind = evt.get("kind")
                    if kind == "MessageEvent" and evt.get("source") == "agent":
                        msg = evt.get("llm_message") or {}
                        parts = msg.get("content") or []
                        text = "".join(p.get("text", "") for p in parts
                                       if isinstance(p, dict) and p.get("type") == "text")
                        if text.strip():
                            last_agent_msg = text
                    elif kind == "ActionEvent":
                        action = evt.get("action") or {}
                        if isinstance(action, dict) and action.get("kind") == "FinishAction":
                            fm = action.get("message")
                            if isinstance(fm, str) and fm.strip():
                                finish_msg = fm
            # stream the step (skip 'finish' — the full answer is delivered below)
            step = parse_line(line)
            if step and on_step and step.type != "finish":
                try:
                    on_step(step)
                except Exception:
                    pass
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log("[chat] timed out")
        proc.kill()
    except Exception as e:
        log(f"[chat] error: {e}")
        try:
            proc.kill()
        except Exception:
            pass
    answer = (last_agent_msg or finish_msg or "").strip()
    if conv_id:
        save_conversation_id(workspace, conv_id)
    if on_answer:
        try:
            on_answer(answer)
        except Exception:
            pass
    return answer, conv_id, bool(answer)
