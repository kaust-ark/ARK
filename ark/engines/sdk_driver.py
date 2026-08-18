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
     max_tokens, max_size, keep_first, sandbox}
stdout: one JSON object per line.
    {"kind": "<EventType>", "text": "..."}      streamed, for the live log
    {"kind": "__result__", "result": ..., "usage": ..., "error_code": ...}
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import sys
from pathlib import Path


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, default=str) + "\n")
    sys.stdout.flush()


#: Delegation tool: spawns a named sub-agent. We register none, so every call
#: it can make comes back "Unknown agent 'X'. Available types: none registered."
#: Offering a tool that cannot succeed is a trap — a capable model ignores it,
#: a weaker one burns its turns on it. Observed on a local 32B, which spent its
#: planning phase trying to delegate to a 'Planner' that does not exist.
_UNUSABLE_TOOLS = {"task_tool_set"}


def _usable_tools(tools: list) -> list:
    """Drop tools that have no chance of succeeding in this configuration."""
    kept = [t for t in tools
            if str(getattr(t, "name", "")) not in _UNUSABLE_TOOLS]
    return kept or tools      # never hand the agent an empty toolbox


def _event_text(event) -> str:
    """Best-effort human text for an SDK event. Never raises.

    A MessageEvent holds nothing readable itself — its words live one hop down,
    at ``llm_message.content``. Reading only the event meant every agent message
    streamed as empty text, so a run whose last word is a message (i.e. any
    model that ends without a FinishAction) reported no result at all despite
    having done the work. Observed on a sandboxed run: 2 MessageEvents, 6
    actions, 6 observations, and a blank result.
    """
    # An ObservationEvent is likewise a wrapper: the tool's reply — including
    # the reason a call was refused — lives at ``observation.content``. Without
    # this hop, a rejected call is recorded as "file_editor:" with nothing after
    # the colon, which names the failure but not its cause.
    for obj in (getattr(event, "llm_message", None),
                getattr(event, "observation", None), event):
        if obj is None:
            continue
        # "detail" and "error" are where the FAILURE events keep their words:
        # ConversationErrorEvent is (code, detail, ...) and AgentErrorEvent is
        # (error, tool_name, ...) — neither has a message/content/text. Omitting
        # them meant every agent failure in this runtime surfaced as
        # "OpenHands error: ConversationRunError — " with nothing after the
        # dash, so a whole run could fail 29 times over and leave no clue why.
        for attr in ("message", "content", "text", "detail", "error"):
            v = getattr(obj, attr, None)
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


def _usage_from_stats(conversation, model):
    """Token/cost totals for a REMOTE conversation.

    The sandboxed path cannot use ``_collect_usage``: a RemoteConversation
    refuses ``persistence_dir`` outright, so there is no ``base_state.json`` on
    this side of the HTTP boundary. The same numbers come back off the wire
    under ``stats.usage_to_metrics``, so only the reader changes.
    """
    try:
        metrics = getattr(conversation.conversation_stats,
                          "usage_to_metrics", None) or {}
        cost = 0.0
        tin = tout = cread = cwrite = 0
        for m in metrics.values():
            cost += float(getattr(m, "accumulated_cost", 0) or 0)
            tu = getattr(m, "accumulated_token_usage", None)
            if tu is None:
                continue
            tin += int(getattr(tu, "prompt_tokens", 0) or 0)
            tout += int(getattr(tu, "completion_tokens", 0) or 0)
            cread += int(getattr(tu, "cache_read_tokens", 0) or 0)
            cwrite += int(getattr(tu, "cache_write_tokens", 0) or 0)
        if not (tin or tout or cost):
            return None
        return {"model": model, "input_tokens": tin, "output_tokens": tout,
                "cache_read_tokens": cread, "cache_creation_tokens": cwrite,
                "cost_usd": cost, "duration_api_ms": 0}
    except Exception:
        return None


def _open_sandbox(sb: dict):
    """Start the agent-server inside Apptainer and return its workspace.

    This is what makes the sandbox an actual boundary. The advisory version
    asked the agent to prefix its commands and was ignored 14 times out of 14
    on project 76759cf7. Here the agent-server itself lives in the container,
    so the process that would run a host command does not exist — there is no
    command the model can choose to type that escapes.
    """
    from openhands.workspace import ApptainerWorkspace

    # The server listens on localhost with NO authentication unless it finds a
    # session key. These are shared HPC nodes, so an unauthenticated port would
    # hand every other user on the box a fully-armed agent. Mint one per run.
    #
    # It travels via APPTAINERENV_* rather than the workspace's own forward_env,
    # which apptainer renders as `--env KEY=value` on the command line — where
    # `ps` shows it to exactly the users it was meant to keep out. (Verified:
    # APPTAINERENV_* still reaches the container under --compat, which implies
    # --cleanenv and strips plain host vars.)
    token = secrets.token_urlsafe(32)
    os.environ["SESSION_API_KEY"] = token
    os.environ["APPTAINERENV_SESSION_API_KEY"] = token

    # ApptainerWorkspace's own mount_dir hardcodes the container path to
    # /workspace; we need the project at its host path instead (see
    # ark.sandbox.structural_sandbox_config), so bind it through the env var
    # apptainer reads directly.
    os.environ["APPTAINER_BIND"] = sb["bind"]

    # detach_logs stays on: the workspace pipes the container's output and only
    # drains that pipe from the logging thread, so turning it off deadlocks the
    # server at the first 64 KB of logs. Its "[APPTAINER] " prefix keeps those
    # lines out of our JSON protocol, which ignores anything not starting "{".
    return ApptainerWorkspace(
        sif_file=sb["sif_file"],
        working_dir=sb["working_dir"],
        detach_logs=True,
    )


def _descendants(pid: int) -> list:
    """Every process under ``pid``, parents before children."""
    children: dict = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text(errors="replace")
            # comm sits in parens and may itself contain spaces or parens, so
            # parse the fields after the LAST ')': state, ppid, ...
            ppid = int(stat[stat.rindex(")") + 1:].split()[1])
        except (OSError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    out, stack = [], [pid]
    while stack:
        for kid in children.get(stack.pop(), []):
            out.append(kid)
            stack.append(kid)
    return out


def _stop_sandbox(workspace) -> None:
    """Stop the container AND everything it spawned.

    ``ApptainerWorkspace.cleanup()`` only SIGTERMs the ``apptainer run`` process
    it started, and that does not bring the container down. Counted on this
    host after four test runs: four full sets of starter / fakeroot shim / tini
    / squashfuse_ll / fuse-overlayfs / agent-server still alive, 17 processes,
    the oldest half an hour after its driver had exited. One leaked agent-server
    per phase on a shared node is how a machine ends up unusable for everyone.

    The tree has to be collected BEFORE cleanup: once the parent dies its
    children are reparented to init and the trail is gone.
    """
    pid = getattr(getattr(workspace, "_process", None), "pid", None)
    tree = _descendants(pid) if pid else []
    try:
        workspace.cleanup()
    except Exception:
        pass
    for victim in reversed(tree + ([pid] if pid else [])):
        try:
            os.kill(victim, signal.SIGKILL)
        except OSError:
            pass


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
        # Free-tier providers meter by the MINUTE (Gemini free: 15 requests
        # and 250K tokens per minute). The SDK default retry budget — 5 tries,
        # ~4.5 minutes of total patience — dies inside one bad window, the
        # conversation dies with it, and the pipeline's outer retry restarts
        # the whole agent call from zero: a restart storm that burns daily
        # quota while completing nothing (watched gemini free do exactly this
        # to the specialization step, one death every ~3 minutes). Ten tries
        # at up to 2 minutes apart outwaits any minute-window quota. Paid
        # providers rarely 429, so for them this changes nothing.
        num_retries=int(cfg.get("num_retries", 10)),
        retry_max_wait=int(cfg.get("retry_max_wait", 120)),
        # Gemini's FREE tier caps cached-content STORAGE per model
        # (TotalCachedContentStorageTokensPerModelFreeTier). With caching on,
        # our large contexts fill that pool within minutes and every request
        # 429s until blobs expire — patience does not help, it is a storage
        # quota, not a rate window. Caching stays on everywhere else: for paid
        # Anthropic/OpenAI it is the difference between 80% and 0% cache hits.
        caching_prompt=bool(cfg.get("caching_prompt", True)),
        # Bound every turn's generation. A hybrid-thinking model (Qwen3) can
        # nondeterministically spend tens of thousands of <think> tokens on
        # one turn; at a local card's ~14 tok/s that is a half-hour of silence,
        # which the SDK's stuck-watchdog rightly executes ("Remote conversation
        # got stuck", c27952f2 — the previous, identical run had sailed
        # through). No agent turn legitimately needs more than 8K of output;
        # a truncated think costs one retry, an unbounded one costs the run.
        max_output_tokens=int(cfg.get("max_output_tokens", 8192)),
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
        tools=_usable_tools(get_default_cli_tools()),
        system_prompt_kwargs={"cli_mode": True},
        condenser=condenser,
    )

    state = {"last_agent_message": "", "finish_message": "",
             "error_code": None, "error_detail": "",
             # Tool calls the environment rejected. An agent that claims
             # success over a failed tool call is the single most damaging
             # thing it can do, because every later phase believes it.
             # Observed on a local 32B: it wrote to "/local_ok.txt", got
             # "Permission denied", and finished with "The file has been
             # created." The run reported success and produced nothing.
             "failed_tools": [],
             # Actions the agent actually took; zero after a completed run is
             # the narrate-instead-of-act signature the nudge below targets.
             "actions": 0}

    def _callback(event) -> None:
        kind = type(event).__name__
        text = _event_text(event)
        _emit({"kind": kind, "text": text[:4000]})
        source = str(getattr(event, "source", ""))
        if kind == "MessageEvent" and "agent" in source:
            if text.strip():
                state["last_agent_message"] = text
        elif kind == "ActionEvent":
            state["actions"] += 1
            # Many models never emit a closing MessageEvent and instead end
            # with a FinishAction carrying the summary. Without this the
            # result comes back empty even on a perfectly successful run.
            action = getattr(event, "action", None)
            if type(action).__name__ == "FinishAction":
                msg = getattr(action, "message", None)
                if isinstance(msg, str) and msg.strip():
                    state["finish_message"] = msg
        elif kind == "ObservationEvent":
            # Record tool calls the environment rejected. These are NOT fatal
            # (the agent may recover), but they must survive to the caller: a
            # model that reports success over a failed tool call otherwise
            # hands the next phase a false premise, and nothing downstream can
            # tell the difference between "wrote the section" and "tried to".
            obs = getattr(event, "observation", None)
            if getattr(obs, "is_error", False):
                tool = str(getattr(event, "tool_name", "") or "tool")
                # 240 not 120: the reason is usually "errno + absolute path",
                # and our project paths alone run ~110 characters — the first
                # cap kept the errno and cut the path, which is the half a
                # post-mortem needs.
                state["failed_tools"].append(f"{tool}: {text[:240]}")
        elif kind == "ConversationErrorEvent":
            # ONLY this one ends a run. Treating every *Error* event as fatal
            # (as this driver first did) kills runs the CLI path survives:
            # AgentErrorEvent is the agent reporting a recoverable problem,
            # such as a tool call that failed, and the loop carries on.
            state["error_code"] = (state["error_code"]
                                   or getattr(event, "code", None) or kind)
            state["error_detail"] = text[:500]

    # Structural sandbox. Failing to start the container drops the phase back
    # onto the host rather than killing it — the sandbox is opt-in and still
    # being proven, and a lost run costs more than an unsandboxed one. The
    # fallback is emitted as an event so it shows up in the run log instead of
    # being discovered months later by counting executions.
    sandbox = cfg.get("sandbox") or None
    workspace = None
    if sandbox:
        try:
            workspace = _open_sandbox(sandbox)
            _emit({"kind": "__sandbox__",
                   "text": f"apptainer: agent confined to {sandbox['sif_file']}"})
        except Exception as e:
            _emit({"kind": "__sandbox__",
                   "text": f"apptainer sandbox FAILED to start ({type(e).__name__}: "
                           f"{str(e)[:300]}) — running on the HOST, unconfined"})
            workspace = None

    # Everything below runs under `finally: cleanup`. The apptainer process is
    # our child but nothing reaps it for us, so any escape from this function
    # that skips cleanup strands a container — and on a shared node those
    # accumulate against every user, not just this run. Conversation() itself
    # can raise (the server rejects the agent spec), which is exactly the path
    # a plain trailing cleanup call would miss.
    rc = 0
    usage = None
    try:
        conversation = Conversation(
            agent=agent,
            workspace=workspace if workspace is not None else cfg["workdir"],
            # A RemoteConversation rejects persistence_dir: its state lives in
            # the container, and usage comes back over the wire instead.
            persistence_dir=(None if workspace is not None
                             else (cfg.get("persistence_dir") or None)),
            callbacks=[_callback],
        )
        try:
            conversation.send_message(cfg["task"])
            conversation.run()
            # One bounded nudge for a turn that PLANNED instead of ACTING. A
            # weaker model regularly answers a large task with a prose plan and
            # no tool call at all; the SDK reads any message without tool calls
            # as the agent's final answer, so the run ends with the plan
            # written and nothing done (observed 5x on a local 32B: ~40s, ~300
            # tokens out, zero actions). Escalations are exempt — an agent
            # that wrote a needs_human report finished on purpose. A model
            # that acted at least once never triggers this.
            if (state["actions"] == 0 and not state["error_code"]
                    and "needs_human" not in (state["last_agent_message"]
                                              + state["finish_message"])):
                _emit({"kind": "__nudge__",
                       "text": "agent produced a plan but zero tool calls — "
                               "sending one continue nudge"})
                conversation.send_message(
                    "You wrote a plan but executed none of it. Do it NOW, in "
                    "this same session: carry out those steps one at a time "
                    "with your terminal and file_editor tools. Do not restate "
                    "the plan. Do not finish until the outputs your task asks "
                    "for actually exist on disk.")
                conversation.run()
        except Exception as e:
            state["error_code"] = state["error_code"] or type(e).__name__
            state["error_detail"] = str(e)[:500]
            rc = 1
        # Read usage HERE, before the finally below stops the container: for a
        # sandboxed run the numbers are an HTTP call to the agent-server, so
        # collecting after cleanup would silently report nothing.
        usage = (_usage_from_stats(conversation, cfg["model"])
                 if workspace is not None
                 else _collect_usage(cfg.get("persistence_dir") or "",
                                     str(getattr(conversation, "id", "")),
                                     cfg["model"]))
    except Exception as e:
        state["error_code"] = state["error_code"] or type(e).__name__
        state["error_detail"] = str(e)[:500]
        rc = 1
    finally:
        if workspace is not None:
            _stop_sandbox(workspace)

    _emit({
        "kind": "__result__",
        "result": state["last_agent_message"] or state["finish_message"],
        "usage": usage,
        "error_code": state["error_code"],
        "error_detail": state["error_detail"],
        "failed_tools": state["failed_tools"][-10:],
    })
    return rc


if __name__ == "__main__":
    sys.exit(main())
