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
from pathlib import Path
from typing import Callable, Optional

from ark.engines.cli import OpenHandsCLI
from ark.observability.steps import parse_line

# conversation id is stored here, inside the project workspace
CONV_FILE = ".ark_chat_conversation"
# DSH reports usage per turn, not cumulatively; the running total lives here
DSH_USAGE_FILE = ".ark_chat_usage.json"
_DSH = "dsh:"
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


class _ChatCLI(OpenHandsCLI):
    """Resume chat using the same process/terminal lifecycle as pipeline agents."""

    def __init__(self, model: str, conversation_id: Optional[str]):
        super().__init__(model)
        self.conversation_id = conversation_id

    def build_command(self, prompt: str, path_boundary: str, code_dir: Path) -> list:
        cmd = ["openhands", "--headless", "--json", "--override-with-envs"]
        if self.conversation_id:
            cmd += ["--resume", self.conversation_id]
        return cmd + ["-t", prompt]


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
    if conversation_id.startswith(_DSH):
        try:
            return json.loads((Path(workspace) / DSH_USAGE_FILE).read_text()).get(conversation_id)
        except (OSError, ValueError):
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

    log(f"[chat] openhands {'resume '+conversation_id if conversation_id else 'new'} "
        f"model={model} ws={workspace}")

    def on_event(line):
        # Skip 'finish': the full answer is delivered below.
        step = parse_line(line)
        if step and on_step and step.type != "finish":
            on_step(step)

    if os.environ.get("ARK_AGENT_RUNTIME", "").strip().lower() == "dsh":
        answer, conv_id = _dsh_turn(workspace, message, model, conversation_id, on_event, log, timeout)
        if conv_id:
            save_conversation_id(workspace, conv_id)
        if on_answer:
            try:
                on_answer(answer)
            except Exception:
                pass
        return answer, conv_id, bool(answer)

    runner = _ChatCLI(model, conversation_id)
    conv_id = conversation_id
    answer = ""
    try:
        _, stdout, _, _, timed_out = runner.execute(
            message, "", workspace, timeout, env=env, on_event=on_event,
            log_fn=lambda msg, level: log(msg),
        )
        if timed_out:
            log("[chat] timed out")
        parsed = runner.parse_output(stdout)
        conv_id = conversation_id or parsed.get("conversation_id")
        answer = (parsed.get("result") or "").strip()
    except Exception as e:
        log(f"[chat] error: {e}")
    if conv_id:
        save_conversation_id(workspace, conv_id)
    if on_answer:
        try:
            on_answer(answer)
        except Exception:
            pass
    return answer, conv_id, bool(answer)


def _dsh_turn(workspace: Path, message: str, model: str, conversation_id: Optional[str],
              on_event: Callable, log: Callable, timeout: int) -> tuple[str, Optional[str]]:
    """One chat turn as the next message of the project's DSH session.

    The session id is stored with a "dsh:" prefix, so an OpenHands id left from
    before a runtime switch starts a new conversation instead of failing.
    """
    from ark.engines.dsh_runtime import DSHRuntime
    sid = conversation_id[len(_DSH):] if conversation_id and conversation_id.startswith(_DSH) else None
    runner = DSHRuntime(model, resume_session_id=sid, next_turn=True)
    runner.agent_type = "chat"
    log(f"[chat] dsh {'resume ' + sid if sid else 'new'} model={model} ws={workspace}")
    try:
        _, stdout, _, _, timed_out = runner.execute(
            message, "", workspace, timeout, on_event=on_event, log_fn=lambda msg, level: log(msg))
    except Exception as e:
        log(f"[chat] error: {e}")
        return "", conversation_id
    if timed_out:
        log("[chat] timed out")
    parsed = runner.parse_output(stdout)
    if parsed.get("error_code"):
        log(f"[chat] {parsed['error_code']}: {(parsed.get('error_detail') or '')[:200]}")
    conv_id = _DSH + runner.session_id if runner.session_id else conversation_id
    if conv_id:
        _add_turn_usage(workspace, conv_id, parsed.get("usage") or {})
    return (parsed.get("result") or "").strip(), conv_id


def _add_turn_usage(workspace: Path, conv_id: str, usage: dict) -> None:
    """Keep a cumulative total per conversation, the shape OpenHands' file has."""
    path = Path(workspace) / DSH_USAGE_FILE
    try:
        totals = json.loads(path.read_text())
    except (OSError, ValueError):
        totals = {}
    cur = totals.get(conv_id) or {k: 0 for k in _USAGE_KEYS} | {"cost_usd": None}
    for k in _USAGE_KEYS:
        cur[k] = int(cur.get(k) or 0) + int(usage.get(k) or 0)
    totals[conv_id] = cur
    try:
        path.write_text(json.dumps(totals))
    except OSError:
        pass
