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
# Calibrated, not guessed. Replaying a real run's event stream at different
# budgets gives total input tokens:
#
#     90k → 100%   50k → 74%   30k → 54%   20k → 39%   12k → 32%   8k → 32%
#
# Two things follow. A generous budget buys almost nothing: at 90k the
# condenser fired in 3 of 15 conversations and the run cost what the stock
# CLI cost. And there is a floor near 32%, because every request must still
# carry the system prompt, the tool definitions and the recent turns, no
# matter how hard history is compacted. Going below that needs fewer turns,
# not a smaller budget.
#
# ~20k sits just above the floor while leaving a working window. The
# absolute ceiling matters as much as the fraction: on a million-token model
# a percentage alone would hand back a budget that never binds.
_BUDGET_FRACTION = float(os.environ.get("ARK_CONTEXT_BUDGET_FRACTION", "0.12"))
_BUDGET_CEILING = int(os.environ.get("ARK_CONTEXT_BUDGET_CEILING", "40000"))
_BUDGET_FLOOR = int(os.environ.get("ARK_CONTEXT_BUDGET_FLOOR", "20000"))
_FALLBACK_CONTEXT_TOKENS = int(os.environ.get("ARK_CONTEXT_FALLBACK_TOKENS", "200000"))
# Secondary guard, for runs that accumulate many tiny events.
_MAX_EVENTS = int(os.environ.get("ARK_CONTEXT_MAX_EVENTS", "120"))
_KEEP_FIRST = 4


def sdk_runtime_enabled() -> bool:
    return os.environ.get("ARK_AGENT_RUNTIME", "cli").strip().lower() == "sdk"


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
    scaled = int(_context_window(model) * _BUDGET_FRACTION)
    return max(_BUDGET_FLOOR, min(scaled, _BUDGET_CEILING))


class OpenHandsSDK(OpenHandsCLI):
    """Same agent, same tools, but the history has a size budget.

    Inherits ``build_env`` (provider key resolution), ``execute`` (streaming,
    watchdog, hard timeout, process-tree kill) and the model helpers from the
    CLI runner, so the only intended difference between the two paths is who
    assembles the conversation.
    """

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
            "api_key": os.environ.get("LLM_API_KEY") or "",
            "base_url": os.environ.get("LLM_BASE_URL") or "",
            "task": f"[SYSTEM RULE] {path_boundary}\n\n{prompt}",
            "workdir": str(code_dir),
            "persistence_dir": str(Path(code_dir) / ".openhands" / "sdk"),
            "max_tokens": history_token_budget(model),
            "max_size": _MAX_EVENTS,
            "keep_first": _KEEP_FIRST,
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
            elif evt.get("kind") == "MessageEvent" and evt.get("text"):
                last_agent_msg = evt["text"]
        if not out["result"]:
            out["result"] = last_agent_msg
        return out
