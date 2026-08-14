"""Standalone OpenHands driver: the agent loop, assembled by us.

Run by the OpenHands interpreter (its own uv tool env), NOT by ark-base.
It therefore imports NOTHING from ark — config arrives as JSON on argv and
results leave as JSON lines on stdout. Keeping the dependency in its own
env is deliberate: pulling the OpenHands stack into the shared platform env
is exactly the kind of dependency mixing that has broken us twice.

Why not just call `openhands --headless`? Because that CLI hardcodes

    LLMSummarizingCondenser(llm=llm, max_size=80, keep_first=4)

leaving `max_tokens` unset. The only live compaction trigger is therefore an
EVENT COUNT, which is blind to size: eighty small events are nothing, eighty
33 KB file observations are millions of tokens. Measured: 12.5M input tokens
for 146k of output on a 2-page paper. The CLI exposes no way to change it,
so we assemble the same agent here — same tools, same summarising condenser,
same loop — and set the token budget the machinery already supports.

Protocol
--------
argv[1]: path to a JSON file with
    {model, api_key, base_url, task, workdir, persistence_dir,
     max_tokens, max_size, keep_first}
stdout: one JSON object per line.
    {"kind": "<EventType>", "text": "..."}      streamed, for the live log
    {"kind": "__result__", "result": ..., "usage": ..., "error_code": ...}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, default=str) + "\n")
    sys.stdout.flush()


def _event_text(event) -> str:
    """Best-effort human text for an SDK event. Never raises."""
    for attr in ("message", "content", "text"):
        v = getattr(event, attr, None)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list):
            parts = []
            for p in v:
                t = getattr(p, "text", None)
                if t is None and isinstance(p, dict):
                    t = p.get("text")
                if t:
                    parts.append(str(t))
            if parts:
                return " ".join(parts)
    return ""


def _collect_usage(persistence_dir: str, conversation_id: str, model: str):
    """Token/cost totals in ARK's usage shape, read from persisted state.

    Read from `base_state.json` rather than the live object: that file is
    where OpenHands records `stats.usage_to_metrics` as plain JSON, and it is
    the same source the CLI runner has always used. Attribute access on the
    in-memory objects silently yielded nothing.
    """
    try:
        base = Path(persistence_dir) / conversation_id / "base_state.json"
        if not base.exists():
            hits = sorted(Path(persistence_dir).glob("*/base_state.json"),
                          key=lambda p: p.stat().st_mtime)
            if not hits:
                return None
            base = hits[-1]
        metrics = (json.loads(base.read_text()).get("stats") or {}).get(
            "usage_to_metrics") or {}
        cost = 0.0
        tin = tout = cread = cwrite = 0
        for m in metrics.values():
            m = m or {}
            cost += float(m.get("accumulated_cost") or 0)
            tu = m.get("accumulated_token_usage") or {}
            tin += int(tu.get("prompt_tokens") or 0)
            tout += int(tu.get("completion_tokens") or 0)
            cread += int(tu.get("cache_read_tokens") or 0)
            cwrite += int(tu.get("cache_write_tokens") or 0)
        if not (tin or tout or cost):
            return None
        return {"model": model, "input_tokens": tin, "output_tokens": tout,
                "cache_read_tokens": cread, "cache_creation_tokens": cwrite,
                "cost_usd": cost, "duration_api_ms": 0}
    except Exception:
        return None


def main() -> int:
    cfg = json.loads(Path(sys.argv[1]).read_text())

    from openhands.sdk import LLM, Agent, Conversation
    from openhands.sdk.context.condenser import LLMSummarizingCondenser
    from openhands_cli.utils import get_default_cli_tools

    llm = LLM(
        model=cfg["model"],
        api_key=cfg.get("api_key") or None,
        base_url=cfg.get("base_url") or None,
        usage_id="agent",
    )
    # The one line this whole driver exists for: compact on SIZE, not on a
    # count of events. max_size stays as a secondary guard against runs that
    # accumulate many tiny events.
    condenser = LLMSummarizingCondenser(
        llm=llm.model_copy(update={"usage_id": "condenser"}),
        max_size=int(cfg.get("max_size", 120)),
        keep_first=int(cfg.get("keep_first", 4)),
        max_tokens=int(cfg["max_tokens"]),
    )
    agent = Agent(
        llm=llm,
        tools=get_default_cli_tools(),
        system_prompt_kwargs={"cli_mode": True},
        condenser=condenser,
    )

    state = {"last_agent_message": "", "finish_message": "",
             "error_code": None, "error_detail": ""}

    def _callback(event) -> None:
        kind = type(event).__name__
        text = _event_text(event)
        _emit({"kind": kind, "text": text[:4000]})
        source = str(getattr(event, "source", ""))
        if kind == "MessageEvent" and "agent" in source:
            if text.strip():
                state["last_agent_message"] = text
        elif kind == "ActionEvent":
            # Many models never emit a closing MessageEvent and instead end
            # with a FinishAction carrying the summary. Without this the
            # result comes back empty even on a perfectly successful run.
            action = getattr(event, "action", None)
            if type(action).__name__ == "FinishAction":
                msg = getattr(action, "message", None)
                if isinstance(msg, str) and msg.strip():
                    state["finish_message"] = msg
        elif kind == "ConversationErrorEvent":
            # ONLY this one ends a run. Treating every *Error* event as fatal
            # (as this driver first did) kills runs the CLI path survives:
            # AgentErrorEvent is the agent reporting a recoverable problem,
            # such as a tool call that failed, and the loop carries on.
            state["error_code"] = (state["error_code"]
                                   or getattr(event, "code", None) or kind)
            state["error_detail"] = text[:500]

    conversation = Conversation(
        agent=agent,
        workspace=cfg["workdir"],
        persistence_dir=cfg.get("persistence_dir") or None,
        callbacks=[_callback],
    )

    rc = 0
    try:
        conversation.send_message(cfg["task"])
        conversation.run()
    except Exception as e:
        state["error_code"] = state["error_code"] or type(e).__name__
        state["error_detail"] = str(e)[:500]
        rc = 1

    _emit({
        "kind": "__result__",
        "result": state["last_agent_message"] or state["finish_message"],
        "usage": _collect_usage(cfg.get("persistence_dir") or "",
                                str(getattr(conversation, "id", "")), cfg["model"]),
        "error_code": state["error_code"],
        "error_detail": state["error_detail"],
    })
    return rc


if __name__ == "__main__":
    sys.exit(main())
