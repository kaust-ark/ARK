"""Standalone DSH SDK process. Imports no platform dependencies or credentials."""
from __future__ import annotations

import importlib.metadata
import json
import os
import signal
import sys

SDK_VERSION = "0.2.1rc2"


def summarize(records, result, model):
    usage = {"model": model, "input_tokens": 0, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_creation_tokens": 0,
             "cost_usd": None, "duration_api_ms": 0, "requests": 0}
    failures, seen = [], set()
    for record in records:
        payload = record.get("payload") or {}
        event = payload.get("event") or {}
        identity = (payload.get("sessionId"), event.get("seq"))
        if not event or identity in seen:
            continue
        seen.add(identity)
        data = event.get("data") or {}
        u = data.get("usage")
        if isinstance(u, dict):
            usage["requests"] += 1
            for dest, source in (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"),
                                 ("cache_read_tokens", "cacheReadTokens"), ("cache_creation_tokens", "cacheWriteTokens")):
                usage[dest] += int(u.get(source) or 0)
        message = data.get("message") or {}
        if event.get("type") == "tool/result" and message.get("isError"):
            failures.append(_text(message.get("content")))
    outcome = dict(result or {})
    if result is None:
        outcome.update(error_code="DSHIncompleteRun", error_detail="DSH exited without a final result")
    outcome.update(usage=usage, failed_tools=failures,
                   sandbox="DSH local execution; filesystem confinement is not provided")
    outcome.setdefault("result", "")
    outcome.setdefault("error_code", None)
    outcome.setdefault("error_detail", None)
    return outcome


def _text(content):
    return "".join(str(x.get("text") or "") for x in (content or []) if isinstance(x, dict))


def _emit(value):
    print(json.dumps(value), flush=True)


def main(config):
    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    result = {"kind": "__result__", "result": "", "session_id": config["session_id"]}
    try:
        version = importlib.metadata.version("deepseek-harness-sdk")
        runtime_version = importlib.metadata.version("deepseek-harness-runtime-bin")
        if version != SDK_VERSION or runtime_version != SDK_VERSION:
            raise ValueError(f"DSH requires SDK and runtime {SDK_VERSION}; got {version} / {runtime_version}")
        from deepseek_harness import DeepSeekHarness

        key = os.environ.get("ARK_DSH_API_KEY")
        if not key:
            raise ValueError("No API key configured for the selected DSH model")
        with DeepSeekHarness(
            cwd=config["workdir"], runtime_cwd=config["workdir"],
            dsh_home=config["dsh_home"], profile="sdk-minimal", patches=(config["patch"],),
            provider=config["provider"], model=config["model_id"], max_tokens=config["max_tokens"],
            api_key=key, base_url=config["base_url"] or None,
            initialize_timeout_seconds=20, request_timeout_seconds=30,
        ) as harness:
            session = (harness.resume_session(config["session_id"]) if config["resume"]
                       else harness.start_session(config["session_id"]))
            output = session.run(config["task"], on_notification=lambda n: _emit(
                {"kind": "DSHEvent", "method": n.method, "payload": n.payload}))
            result["finish_reason"] = output.finish_reason
            if output.finish_reason == "completed" and output.final_response.strip():
                result["result"] = output.final_response.strip()
            else:
                reason = next((e.get("data", {}).get("reason") for e in reversed(output.events)
                               if e.get("type") == "turn/end"), None)
                result.update(error_code="DSHIncompleteRun",
                              error_detail=json.dumps(reason) if reason else "DSH returned no completed response")
    except KeyboardInterrupt:
        result.update(error_code="DSHCancelled", error_detail="DSH execution was cancelled")
    except (ImportError, importlib.metadata.PackageNotFoundError, ValueError) as exc:
        result.update(error_code="DSHConfigurationError", error_detail=(
            f"{exc}. Install deepseek-harness-sdk=={SDK_VERSION} in an isolated environment "
            "and set ARK_DSH_PYTHON to its Python executable."))
    except Exception as exc:
        result.update(error_code="DSHRuntimeError", error_detail=f"{type(exc).__name__}: {exc}")
    _emit(result)
    return 1 if result.get("error_code") else 0


if __name__ == "__main__":
    with open(sys.argv[1]) as stream:
        configuration = json.load(stream)
    raise SystemExit(main(configuration))
