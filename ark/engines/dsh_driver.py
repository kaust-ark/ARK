"""Standalone DSH SDK process. Imports no platform dependencies or credentials."""
from __future__ import annotations

import importlib.metadata
import json
import os
import queue
import signal
import sys
import time

SDK_VERSION = "0.2.1rc2"

# DSH failure codes after its own retries (packages/llm/llm/src/error.ts,
# llm-deepseek transport.ts, llm-pi-ai stream.ts at rc.2). The names ending in
# RateLimitError / ServiceUnavailableError are ones ARK's classifier already
# retries (engines/__init__.py _TRANSIENT); MaxIterationsReached is its
# "used the budget, keep the work" path.
_TRANSIENT_CODES = {"RATE_LIMIT": "DSH.RateLimitError", "SERVER": "DSH.ServiceUnavailableError",
                    "TIMEOUT": "DSH.ServiceUnavailableError", "TRANSPORT": "DSH.ServiceUnavailableError",
                    "EMPTY_RESPONSE": "DSH.ServiceUnavailableError"}
_BUDGET_CODES = {"CONTEXT_WINDOW_EXCEEDED"}
_QUOTA_CODES = {"QUOTA", "ACCOUNT_QUOTA"}

_CONTINUE = ("Your last reply hit the output-token limit. Continue exactly where you "
             "stopped. Keep it shorter: put long content in files, not in the reply.")
_NUDGE = ("You ended your turn without doing anything. Carry out the task now with "
          "your tools, then reply with a short summary of what you did.")
_SUMMARY = ("Reply with a short summary: what you did, which files you changed or "
            "wrote, and what is still open. Do not start new work.")


def _usage_of(event):
    """Billed usage carried by one session event, if any.

    A committed answer carries `data.usage`. An attempt the runtime discarded
    (a retry, an overflow) carries it only inside its stream chunks, and the
    provider bills it all the same.
    """
    data = event.get("data") or {}
    if isinstance(data.get("usage"), dict):
        return data["usage"]
    if event.get("type") == "assistant/attempt":
        last = None
        for part in data.get("stream") or []:
            u = (part.get("chunk") or {}).get("usage") if isinstance(part, dict) else None
            if isinstance(u, dict):
                last = u
        return last
    return None


def summarize(records, result, model):
    usage = {"model": model, "input_tokens": 0, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_creation_tokens": 0,
             "cost_usd": None, "duration_api_ms": 0, "requests": 0,
             "discarded_attempts": 0}
    failures, seen = [], set()
    for record in records:
        payload = record.get("payload") or {}
        event = payload.get("event") or {}
        identity = (payload.get("sessionId"), event.get("seq"))
        if not event or identity in seen:
            continue
        seen.add(identity)
        u = _usage_of(event)
        if isinstance(u, dict):
            usage["requests"] += 1
            if event.get("type") == "assistant/attempt":
                usage["discarded_attempts"] += 1
            # DSH counts are disjoint: inputTokens excludes cache reads.
            for dest, source in (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"),
                                 ("cache_read_tokens", "cacheReadTokens"), ("cache_creation_tokens", "cacheWriteTokens")):
                usage[dest] += int(u.get(source) or 0)
        message = (event.get("data") or {}).get("message") or {}
        if event.get("type") == "tool/result" and message.get("isError"):
            failures.append(_text(message.get("content")))
    outcome = dict(result or {})
    if result is None:
        outcome.update(error_code="DSH.ServiceUnavailableError",
                       error_detail="DSH exited without a final result")
    outcome.update(usage=usage, failed_tools=failures)
    outcome.setdefault("sandbox", "DSH: no sandbox report")
    outcome.setdefault("result", "")
    outcome.setdefault("error_code", None)
    outcome.setdefault("error_detail", None)
    return outcome


def classify(reason):
    """ARK error code for a DSH turn ending, or None when the turn did its job."""
    kind = (reason or {}).get("kind")
    if kind in (None, "completed"):
        return None, None
    if kind == "max-tokens":
        return "DSH.MaxIterationsReached", "output-token limit reached twice in a row"
    if kind in ("aborted", "interrupted"):
        return "DSH.Cancelled", json.dumps(reason)
    if kind == "error":
        err = reason.get("error") or {}
        code = str(err.get("code") or "UNKNOWN")
        status = err.get("status")
        message = str(err.get("message") or "")
        detail = f"{code} (HTTP {status}): {message}"
        if code in _QUOTA_CODES or str(status) == "402" or message.lstrip().startswith("402:"):
            return "DSH.QuotaExceeded", detail
        if code in _TRANSIENT_CODES:
            return _TRANSIENT_CODES[code], detail
        if code in _BUDGET_CODES:
            return "DSH.MaxIterationsReached", detail
        return f"DSH.{code}", detail
    return f"DSH.{kind}", json.dumps(reason)


def _text(content):
    return "".join(str(x.get("text") or "") for x in (content or []) if isinstance(x, dict))


def _emit(value):
    print(json.dumps(value), flush=True)


def _sandbox_note(events):
    mode = next((e.get("data") for e in events if e.get("type") == "sandbox/mode"), None)
    if not mode:
        return "DSH: no sandbox report"
    detail = json.dumps(mode)
    if "danger-full-access" in detail:
        return "DSH sandbox: none (danger-full-access)"
    return f"DSH sandbox: {detail[:200]}"


class _Turn:
    """Drives one prompt to quiescence: the session idle with no job open.

    Session.run() returns at the first idle, while a background job the turn
    started may still be running; closing the harness then kills the job
    silently and the turn its completion would have woken is never seen.
    """

    def __init__(self, harness, session_id, deadline, max_wakes):
        self.h, self.sid, self.deadline, self.max_wakes = harness, session_id, deadline, max_wakes
        self.events, self.open_jobs, self.turn_ends = [], set(), []
        self.text, self.tool_calls = "", 0

    def run(self, prompt):
        """Returns a stop reason: quiescent, wall-clock or wake-budget."""
        client = self.h.client
        sub = client.subscribe_session_notifications(self.sid)
        try:
            message_id = client.session_prompt(self.sid, [{"type": "text", "text": prompt}],
                                               notification_subscription=sub)
            received, ends_before, text, calls = False, len(self.turn_ends), "", 0
            while True:
                if time.monotonic() >= self.deadline:
                    return "wall-clock"
                try:
                    n = sub._notifications.get(timeout=1.0)  # rc.2 next() has no timeout
                except queue.Empty:
                    continue
                if isinstance(n, BaseException):
                    raise n
                _emit({"kind": "DSHEvent", "method": n.method, "payload": n.payload})
                p = n.payload or {}
                if n.method == "session.event" and p.get("sessionId") == self.sid:
                    ev = p.get("event") or {}
                    ty, data = ev.get("type"), ev.get("data") or {}
                    self.events.append(ev)
                    if not received:
                        received = ty == "agent/inbox/spliced" and any(
                            m.get("id") == message_id for m in data.get("inserted") or [])
                        continue
                    if ty == "job/registered":
                        self.open_jobs.add(data.get("jobId"))
                    elif ty == "job/settled":
                        self.open_jobs.discard(data.get("jobId"))
                    elif ty == "tool/call":
                        calls += 1
                    elif ty == "assistant/message":
                        t = _text((data.get("message") or {}).get("content"))
                        if t.strip():
                            text = t
                    elif ty == "turn/end":
                        self.turn_ends.append(data.get("reason") or {})
                elif n.method == "session.status" and p.get("sessionId") == self.sid and received:
                    if p.get("status") == "idle":
                        self.text = text.strip()
                        self.tool_calls += calls
                        if not self.open_jobs:
                            return "quiescent"
                        if len(self.turn_ends) - ends_before > self.max_wakes:
                            return "wake-budget"
        finally:
            sub.close()


def main(config):
    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    result = {"kind": "__result__", "result": "", "session_id": config["session_id"]}
    turn = None
    try:
        version = importlib.metadata.version("deepseek-harness-sdk")
        runtime_version = importlib.metadata.version("deepseek-harness-runtime-bin")
        if version != SDK_VERSION or runtime_version != SDK_VERSION:
            raise ValueError(f"DSH requires SDK and runtime {SDK_VERSION}; got {version} / {runtime_version}")
        from deepseek_harness import DeepSeekHarness

        key = os.environ.get("ARK_DSH_API_KEY")
        if not key:
            raise ValueError("No API key configured for the selected DSH model")
        deadline = time.monotonic() + float(config.get("wall_s") or 1e9)
        with DeepSeekHarness(
            cwd=config["workdir"], runtime_cwd=config["workdir"],
            dsh_home=config["dsh_home"], profile=config.get("profile", "sdk"),
            patches=tuple(config["patches"]),
            provider=config["provider"], model=config["model_id"], max_tokens=config["max_tokens"],
            reasoning_effort=config.get("reasoning_effort"),
            # Gateway routes read the key from apiKeyEnv; passing it here would
            # also export DEEPSEEK_API_KEY to the runtime for every route.
            api_key=key if config["provider"] == "deepseek-official" else None,
            base_url=(config["base_url"] or None) if config["provider"] == "deepseek-official" else None,
            initialize_timeout_seconds=60,
            # Resume replays the whole log; a multi-hour session can take more
            # than a fixed RPC timeout. ARK's outer wall clock bounds it.
            request_timeout_seconds=None,
        ) as harness:
            session = (harness.resume_session(config["session_id"]) if config["resume"]
                       else harness.start_session(config["session_id"]))
            turn = _Turn(harness, session.id, deadline, int(config.get("max_wakes") or 3))
            stop_reason = turn.run(config["task"])
            followups = []
            # One in-session follow-up per kind, never a fresh session: the
            # model keeps everything it did, and nothing is re-run.
            while stop_reason == "quiescent" and len(followups) < 2:
                reason = turn.turn_ends[-1] if turn.turn_ends else {}
                if reason.get("kind") == "max-tokens" and "continue" not in followups:
                    followups.append("continue")
                    stop_reason = turn.run(_CONTINUE)
                elif reason.get("kind") == "completed" and not turn.text and "nudge" not in followups \
                        and "summary" not in followups:
                    pick = "nudge" if (config.get("acts") and not turn.tool_calls) else "summary"
                    followups.append(pick)
                    stop_reason = turn.run(_NUDGE if pick == "nudge" else _SUMMARY)
                else:
                    break
            reason = turn.turn_ends[-1] if turn.turn_ends else {}
            result.update(finish_reason=reason.get("kind"), stop_reason=stop_reason,
                          followups=followups, open_jobs=sorted(turn.open_jobs),
                          tool_calls=turn.tool_calls, sandbox=_sandbox_note(turn.events))
            if stop_reason != "quiescent":
                # Out of time (or of wake turns) with work still running: keep
                # what was done, report the jobs the close is about to end.
                result.update(error_code="DSH.MaxIterationsReached",
                              error_detail=f"{stop_reason}; open jobs: {sorted(turn.open_jobs)}")
            else:
                code, detail = classify(reason)
                result.update(error_code=code, error_detail=detail)
            result["result"] = turn.text
    except KeyboardInterrupt:
        result.update(error_code="DSH.Cancelled", error_detail="DSH execution was cancelled")
    except (ImportError, importlib.metadata.PackageNotFoundError, ValueError) as exc:
        result.update(error_code="DSHConfigurationError", error_detail=(
            f"{exc}. Install deepseek-harness-sdk=={SDK_VERSION} in an isolated environment "
            "and set ARK_DSH_PYTHON to its Python executable."))
    except Exception as exc:
        # The runtime process died or closed its transport: retryable once,
        # and the retry resumes this session.
        name = type(exc).__name__
        code = "DSH.ServiceUnavailableError" if "Transport" in name else "DSHRuntimeError"
        result.update(error_code=code, error_detail=f"{name}: {exc}")
    if turn is not None and not result.get("result"):
        result["result"] = turn.text
    _emit(result)
    return 1 if result.get("error_code") and result["error_code"] != "DSH.MaxIterationsReached" else 0


if __name__ == "__main__":
    with open(sys.argv[1]) as stream:
        configuration = json.load(stream)
    raise SystemExit(main(configuration))
