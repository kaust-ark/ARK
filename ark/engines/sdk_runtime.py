"""OpenHands runtime with a size-aware context budget.

Why this exists
---------------
We drive OpenHands through its headless CLI, which hardcodes

    LLMSummarizingCondenser(llm=llm, max_size=80, keep_first=4)

and leaves ``max_tokens`` unset. The only live compaction trigger is
therefore an EVENT COUNT, which is blind to size: eighty small events are
nothing, eighty 33 KB file observations are millions of tokens, and the
condenser never notices. Measured consequence: a 2-page paper on the
cheapest model burned 12.5M input tokens to produce 146k of output (85:1),
and a real run reached 34.2M. We pay almost entirely to re-send history.

The CLI exposes no knob for this, so we assemble the same agent ourselves
(``sdk_driver.py``) and set the token budget its machinery already
supports. Nothing is reimplemented: same tools, same summarising condenser,
same conversation loop.

The driver runs under OpenHands' OWN interpreter rather than in-process.
Importing the OpenHands stack into the shared platform env would mean
mixing its pinned dependencies with ours, which is precisely what broke the
platform twice before. A subprocess we fully control gives the same control
point with none of that risk, and reuses the streaming/timeout/kill
plumbing that already works.

Selection is by env var so both paths stay runnable and comparable on the
same project:

    ARK_AGENT_RUNTIME=sdk   → this runtime
    ARK_AGENT_RUNTIME=cli   → the stock headless CLI (default)

Assembling the Conversation ourselves also gives us somewhere to put a
workspace, which is what the structural Apptainer sandbox needs
(``ARK_AGENT_SANDBOX=apptainer``, off by default — see ``ark.sandbox``).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from ark.engines.cli import OpenHandsCLI

_DRIVER = Path(__file__).parent / "sdk_driver.py"

# How much conversation history may accumulate before compaction.
#
# MEASURED, and the measurement overturned the idea this runtime was built
# on. Two A/B runs of the same 2-page paper against the stock CLI baseline
# (12.5M input tokens, $13.19, score 6.8):
#
#     90k budget → 11.4M tokens, $11.99   (compaction fired 3/15 sessions)
#     24k budget → 27.6M tokens, $29.75   (compaction fired 4/9; run died)
#
# Compacting harder made it more than twice as expensive. Sessions in the
# 24k run reached 463 and 408 agent actions with 37 and 92 compactions:
# starved of its working context, the agent re-read files, re-ran commands
# and re-planned. Turn count exploded, and total input with it.
#
# An offline replay of the event stream had predicted 20k would cost 39% of
# 90k. That prediction was worthless because it assumed the event stream is
# independent of the budget. It is not — the budget changes what the agent
# does, and that feedback dominates everything else.
#
# So: a generous budget only, as a ceiling against pathological histories,
# never as a cost lever. The real lever is turn count, which this knob
# cannot touch.
_BUDGET_FRACTION = float(os.environ.get("ARK_CONTEXT_BUDGET_FRACTION", "0.45"))
_BUDGET_CEILING = int(os.environ.get("ARK_CONTEXT_BUDGET_CEILING", "150000"))
_BUDGET_FLOOR = int(os.environ.get("ARK_CONTEXT_BUDGET_FLOOR", "60000"))
_FALLBACK_CONTEXT_TOKENS = int(os.environ.get("ARK_CONTEXT_FALLBACK_TOKENS", "200000"))
# Share of the model's window history may occupy; the rest is the reply and the
# system prompt. Only binds on models smaller than the floor.
_WINDOW_SAFETY = float(os.environ.get("ARK_CONTEXT_WINDOW_SAFETY", "0.8"))
# Secondary guard, for runs that accumulate many tiny events.
_MAX_EVENTS = int(os.environ.get("ARK_CONTEXT_MAX_EVENTS", "120"))
_KEEP_FIRST = 4


def sdk_runtime_enabled() -> bool:
    return os.environ.get("ARK_AGENT_RUNTIME", "cli").strip().lower() == "sdk"


def _sandbox_config(code_dir: Path) -> Optional[dict]:
    """Structural-sandbox settings for the driver, or None.

    Swallowing the error is deliberate. This runtime already promises that a
    broken helper cannot stop a run, and an unsandboxed phase is a far smaller
    failure than a phase that never starts.
    """
    try:
        from ark.sandbox import structural_sandbox_config
        return structural_sandbox_config(code_dir)
    except Exception:
        return None


def openhands_python() -> Optional[str]:
    """Interpreter that can import the OpenHands SDK, or None.

    The `openhands` console script is a shebang pointing at the interpreter
    of its own (uv tool) environment, so we read it from there rather than
    guessing at install layouts.
    """
    override = os.environ.get("ARK_OPENHANDS_PYTHON")
    if override and Path(override).exists():
        return override
    script = shutil.which("openhands")
    if not script:
        return None
    try:
        first = Path(script).read_text(errors="replace").splitlines()[0]
    except OSError:
        return None
    if first.startswith("#!"):
        interp = first[2:].strip().split()[0]
        if Path(interp).exists():
            return interp
    return None


def _context_window(model: str) -> int:
    """Input-token window for ``model``, or a conservative fallback."""
    try:
        import litellm
        info = litellm.get_model_info(model)
        window = info.get("max_input_tokens") or info.get("max_tokens")
        if window:
            return int(window)
    except Exception:
        pass
    return _FALLBACK_CONTEXT_TOKENS


def history_token_budget(model: str) -> int:
    """Tokens of conversation history allowed before compacting."""
    window = _context_window(model)
    scaled = int(window * _BUDGET_FRACTION)
    budget = max(_BUDGET_FLOOR, min(scaled, _BUDGET_CEILING))
    # The floor keeps a large model from being starved into repeating itself.
    # It must not, however, promise a SMALL model more history than its context
    # can physically hold: a 32k local model given the 60k floor never reaches
    # the compaction trigger at all, so history grows until the request itself
    # overflows the window. Cap by the window, leaving room for the reply.
    return min(budget, int(window * _WINDOW_SAFETY)) if window else budget


class OpenHandsSDK(OpenHandsCLI):
    """Same agent, same tools, but the history has a size budget.

    Inherits ``build_env`` (provider key resolution), ``execute`` (streaming,
    watchdog, hard timeout, process-tree kill) and the model helpers from the
    CLI runner, so the only intended difference between the two paths is who
    assembles the conversation.
    """

    def _resolve_api_key(self, model: str) -> str:
        """The provider key for ``model``, by the same rule as ``build_env``.

        Prefers <PROVIDER>_API_KEY (openrouter/… → OPENROUTER_API_KEY, which is
        what the launcher actually sets) and accepts a pre-set LLM_API_KEY for
        callers that export it directly.
        """
        try:
            from ark.llm_lite import provider_key_env
            provider = model.split("/", 1)[0] if "/" in model else ""
            if provider:
                key = os.environ.get(provider_key_env(provider))
                if key:
                    return key
        except Exception:
            pass
        return os.environ.get("LLM_API_KEY") or ""

    def build_command(self, prompt: str, path_boundary: str, code_dir: Path) -> list:
        py = openhands_python()
        if not py:
            # No SDK interpreter: fall back to the stock CLI command rather
            # than failing the phase. The budget is an optimisation, not a
            # correctness requirement.
            return super().build_command(prompt, path_boundary, code_dir)

        model = self._llm_model()
        cfg = {
            "model": model,
            # Resolve the key the same way build_env does, rather than reading
            # LLM_API_KEY out of the parent environment. LLM_API_KEY is an
            # OUTPUT of build_env — it maps <PROVIDER>_API_KEY onto the name
            # the CLI child expects — so reading it here found a value only
            # when someone happened to export it globally. Everywhere else the
            # SDK runtime ran with api_key="" and every agent died on a 401
            # whose reason never reached the log ("ConversationRunError —"
            # with nothing after the dash). The pipeline then logged each dead
            # agent as "✓ completed" and wrote a paper with no experiments.
            "api_key": self._resolve_api_key(model),
            "base_url": os.environ.get("LLM_BASE_URL") or "",
            "task": f"[SYSTEM RULE] {path_boundary}\n\n{prompt}",
            "workdir": str(code_dir),
            "persistence_dir": str(Path(code_dir) / ".openhands" / "sdk"),
            "max_tokens": history_token_budget(model),
            "max_size": _MAX_EVENTS,
            "keep_first": _KEEP_FIRST,
            "sandbox": _sandbox_config(code_dir),
            # Gemini free tier caps cached-content STORAGE (a pool, not a rate
            # window); with caching on, agent-sized contexts fill it within
            # minutes and every call 429s until blobs expire. Cache stays on
            # for every other provider — it is 80% of our input tokens.
            "caching_prompt": not model.startswith("gemini/"),
        }
        # 0600 so the API key never sits world-readable; removed by execute().
        fd, path = tempfile.mkstemp(prefix="ark-sdk-", suffix=".json")
        os.close(fd)
        Path(path).chmod(0o600)
        Path(path).write_text(json.dumps(cfg))
        self._config_path = path
        return [py, str(_DRIVER), path]

    def execute(self, *args, **kwargs):
        try:
            return super().execute(*args, **kwargs)
        finally:
            path = getattr(self, "_config_path", None)
            if path:
                # Carries the API key — never leave it behind.
                Path(path).unlink(missing_ok=True)
                self._config_path = None

    def parse_output(self, stdout: str) -> dict:
        """Read the driver's JSON lines; the last __result__ wins."""
        out = {"result": "", "usage": None, "error_code": None, "error_detail": None}
        last_agent_msg = ""
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
            except Exception:
                continue
            if evt.get("kind") == "__result__":
                out["result"] = evt.get("result") or ""
                out["usage"] = evt.get("usage")
                out["error_code"] = evt.get("error_code")
                out["error_detail"] = evt.get("error_detail")
                out["failed_tools"] = evt.get("failed_tools") or []
            elif evt.get("kind") == "__sandbox__":
                # Per-call proof that confinement actually happened. The
                # pipeline's one-time "sandbox: STRUCTURAL" banner reports a
                # CAPABILITY check made before any agent ran; it says the image
                # is present, not that this call went into it. The driver emits
                # this event either way — it also reports a sandbox that failed
                # to start and fell back to the host — and nothing was reading
                # it, so a run could degrade to unconfined execution with the
                # banner still claiming otherwise.
                out["sandbox"] = evt.get("text") or ""
            elif evt.get("kind") == "MessageEvent" and evt.get("text"):
                last_agent_msg = evt["text"]
        if not out["result"]:
            out["result"] = last_agent_msg
        return out
