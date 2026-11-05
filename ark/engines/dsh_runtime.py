"""Opt-in DSH runner, using an independently installed, pinned SDK interpreter."""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path

from .cli import AgentCLI
from .dsh_driver import SDK_VERSION, summarize
from .dsh_profile import PROFILE, role_patch
from ark.llm_lite import resolve_key

# Roles whose job is an answer, not an action on files (sdk_runtime.py keeps
# the same list): an answer with no tool calls is not "did nothing".
_ANSWER_ROLES = frozenset({"planner", "researcher", "reviewer", "meta_debugger"})

# DSH-specific working rule, added to the task. The model learns about jobs
# from DSH's own tool guidance, but in E1 it still blocked on job_output after
# a promoted command instead of ending its turn, which turns a long wait into
# one request every ten minutes.
_RUNTIME_NOTE = (
    "\n\n## Long-running commands\n"
    "Start anything that takes more than a few minutes with the bash tool's "
    "`run_in_background` option, note the job id, and end your turn. You are woken "
    "when the job finishes. Do not poll, sleep, or block on `job_output` to wait."
)

_RESUME_NOTE = (
    "[Resumed] Your previous attempt at this task was interrupted ({why}). The task "
    "itself is unchanged and is earlier in this conversation. Check what is already "
    "done on disk before redoing anything, then continue it."
)


def _history_budget(model: str, window: int) -> int:
    from .sdk_runtime import _BUDGET_CEILING, _BUDGET_FLOOR, _BUDGET_FRACTION, history_token_budget
    if os.environ.get("ARK_DSH_CONTEXT_WINDOW"):
        return max(_BUDGET_FLOOR, min(int(window * _BUDGET_FRACTION), _BUDGET_CEILING))
    return history_token_budget(model)


def price(model: str, usage: dict):
    """(USD, source) for one call's DSH usage, or (None, None) when unknown.

    DSH reports tokens only. The explicit ARK_DSH_PRICE_PER_M ("input,
    cache_read,output", USD per million) wins; otherwise litellm's table, the
    same source the OpenHands path prices with, so the two runtimes compare.
    """
    uncached = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cache_read_tokens") or 0) + int(usage.get("cache_creation_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    explicit = os.environ.get("ARK_DSH_PRICE_PER_M", "").strip()
    if explicit:
        p_in, p_cache, p_out = (float(x) for x in explicit.split(","))
        return (uncached * p_in + cached * p_cache + out * p_out) / 1e6, "ARK_DSH_PRICE_PER_M"
    try:
        import litellm
        prompt, completion = litellm.cost_per_token(
            model=model, prompt_tokens=uncached + cached, completion_tokens=out,
            cache_read_input_tokens=int(usage.get("cache_read_tokens") or 0))
        return prompt + completion, "litellm"
    except Exception:
        return None, None


def _window(model: str) -> int:
    explicit = os.environ.get("ARK_DSH_CONTEXT_WINDOW", "").strip()
    if explicit:
        return int(explicit)
    from .sdk_runtime import _context_window
    return _context_window(model)


class DSHRuntime(AgentCLI):
    def __init__(self, model_name, model_variant=None, *, resume_session_id=None, resume_reason="",
                 next_turn=False):
        super().__init__(model_name, model_variant)
        self.resume_session_id = resume_session_id
        self.resume_reason = resume_reason
        # A conversation's next message (chat), not a recovery of a cut task.
        self.next_turn = next_turn
        self.session_id = None
        self._timeout = None

    # A retry can carry on in the same session instead of starting over.
    supports_resume = True
    # On SIGTERM the driver closes the session, which ends its shells, and
    # reports what the call used. Killed outright it reports nothing.
    graceful_stop_s = 5

    def build_env(self, code_dir=None):
        env = super().build_env(code_dir)
        model = self.model_variant or self.model_name
        key = resolve_key(model) or os.environ.get("LLM_API_KEY")
        # Only the selected model's credential crosses into the agent process.
        for name in list(env):
            if (any(part in name.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
                    or name.startswith(("AWS_", "AZURE_", "DSH_"))
                    or name in {"SSH_AUTH_SOCK", "GOOGLE_APPLICATION_CREDENTIALS"}):
                env.pop(name, None)
        if key:
            env["ARK_DSH_API_KEY"] = key
        # The runtime is a single-file executable that unpacks ~70 MB of native
        # modules under $HOME/.cache, and HOME is the project directory. One
        # shared, versioned cache instead of a copy in every project.
        env["PKG_NATIVE_CACHE_PATH"] = os.environ.get("ARK_DSH_PKG_CACHE") or str(
            Path.home() / ".cache" / "idea2paper" / f"dsh-pkg-{SDK_VERSION}")
        # TMPDIR is the project in production. The runtime puts spill files
        # and native-loader dirs there, so give it a corner of its own.
        if code_dir is not None:
            tmp = Path(code_dir).resolve() / ".dsh" / "tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            env["TMPDIR"] = str(tmp)
        return env

    def build_command(self, prompt, path_boundary, code_dir):
        # Configuration is consumed by our child driver, never by a shell.
        # It contains no API key, and is private even when prompts are sensitive.
        workdir = Path(code_dir).resolve()
        model = self.model_variant or self.model_name
        prefix, sep, model_id = model.partition("/")
        if not sep or not model_id:
            raise ValueError("DSH requires a provider/model identifier")
        if os.environ.get("ARK_AGENT_SANDBOX", "").strip().lower() == "apptainer":
            raise ValueError("DSH Apptainer execution is not implemented; use the existing SDK runtime")
        protocol = os.environ.get("ARK_DSH_API", "").strip()
        endpoint = os.environ.get("LLM_BASE_URL", "").strip()
        custom = bool(protocol) or prefix != "deepseek"
        if custom:
            protocol = protocol or ("openai-completions" if prefix == "openai" else "")
            if protocol not in {"openai-completions", "openai-responses", "anthropic-messages"}:
                raise ValueError("Set ARK_DSH_API to the gateway's supported API protocol")
            if not endpoint:
                raise ValueError("DSH custom providers require an explicit LLM_BASE_URL")
        window = _window(model)
        max_tokens = int(os.environ.get("ARK_DSH_MAX_TOKENS", "16384"))
        if not 0 < max_tokens < window:
            raise ValueError("DSH requires 0 < ARK_DSH_MAX_TOKENS < the context window")

        sid = self.resume_session_id or uuid.uuid4().hex
        if not re.fullmatch(r"[a-f0-9]{32}", sid):
            raise ValueError("Invalid DSH session id")
        root = workdir / ".dsh" / sid
        root.resolve().relative_to(workdir)
        manifest = root / "run.json"
        if self.resume_session_id:
            saved = json.loads(manifest.read_text())
            if saved.get("workdir") != str(workdir) or saved.get("model") != model:
                raise ValueError("DSH resume requires the original workspace and model")
            if saved.get("endpoint") != endpoint or saved.get("protocol") != protocol:
                raise ValueError("DSH resume requires the original provider configuration")
            if saved.get("sdk_version") != SDK_VERSION:
                raise ValueError("DSH resume requires the SDK version that wrote the session")
        else:
            root.mkdir(parents=True, mode=0o700, exist_ok=False)
            _write_private(manifest, {"session_id": sid, "workdir": str(workdir),
                                     "model": model, "endpoint": endpoint, "protocol": protocol,
                                     "sdk_version": SDK_VERSION})
        self.session_id = sid
        provider = "ark-gateway" if custom else "deepseek-official"
        role = getattr(self, "agent_type", None) or "research"
        skills = workdir / ".claude" / "skills"
        patch = role_patch(
            role=role, workdir=workdir, provider=provider, model_id=model_id, window=window,
            max_tokens=max_tokens, history_budget=_history_budget(model, window),
            api=protocol, endpoint=endpoint,
            sandbox=os.environ.get("ARK_DSH_SANDBOX", "danger-full-access").strip(),
            skills_dir=skills, retries=int(os.environ.get("ARK_DSH_RETRIES", "3")))
        _write_private(root / "runtime.patch.yml", patch)
        if self.resume_session_id and self.next_turn:
            task = prompt
        elif self.resume_session_id:
            task = _RESUME_NOTE.format(why=self.resume_reason or "unknown cause")
            deadline = prompt.rfind("\n\n## Time limit\n")
            if deadline >= 0:
                task += prompt[deadline:]
        else:
            task = f"[SYSTEM RULE] {path_boundary}\n\n{prompt}{_RUNTIME_NOTE}"
        timeout = self._timeout or 0
        config = {"session_id": sid, "resume": bool(self.resume_session_id),
                  "model": model, "model_id": model_id, "provider": provider, "profile": PROFILE,
                  "workdir": str(workdir), "dsh_home": str(root / "home"),
                  "patches": [str(root / "runtime.patch.yml")], "base_url": endpoint,
                  "max_tokens": max_tokens,
                  "reasoning_effort": os.environ.get("ARK_DSH_EFFORT", "").strip() or None,
                  "acts": role not in _ANSWER_ROLES,
                  # Stop on our own a little before the hard kill, so the
                  # result, the usage and the open jobs are reported.
                  "wall_s": timeout - min(120, max(2, timeout // 10)) if timeout else None,
                  "task": task}
        self._config_path = root / "request.json"
        _write_private(self._config_path, config)
        return [os.environ.get("ARK_DSH_PYTHON") or sys.executable,
                str(Path(__file__).with_name("dsh_driver.py")), str(self._config_path)]

    def execute(self, prompt, path_boundary, code_dir, timeout, log_fn=None, on_event=None, env=None):
        aborted = False
        self._timeout = timeout

        def observe(line):
            nonlocal aborted
            verdict = on_event(line)
            if verdict == "ABORT":
                aborted = True
            return verdict

        try:
            result = super().execute(prompt, path_boundary, code_dir, timeout, log_fn=log_fn,
                                     on_event=observe if on_event else None, env=env)
            rc, stdout, stderr, elapsed, timed_out = result
            reported = "__result__" in stdout
            if not reported or (rc not in (0, None) and not self.parse_output(stdout).get("error_code")):
                # Killed before the driver could report, or a success report
                # from a process that then failed. The hard wall clock is a used
                # budget (keep the work, no retry), a breaker abort is a
                # cancellation, and anything else is a crashed runtime, which a
                # retry resumes. A partial message never counts as done.
                code = ("DSH.MaxIterationsReached" if timed_out else
                        "DSH.Cancelled" if aborted else "DSH.ServiceUnavailableError")
                stdout += "\n" + json.dumps({"kind": "__result__", "error_code": code,
                                              "error_detail": f"DSH process exited with code {rc}"}) + "\n"
            return rc, stdout, stderr, elapsed, timed_out
        except (OSError, ValueError, KeyError) as exc:
            # Return a terminal structured error so the orchestrator does not
            # retry an absent SDK or invalid configuration four times.
            return 1, json.dumps({"kind": "__result__", "error_code": "DSHConfigurationError",
                                  "error_detail": str(exc)}), "", 0, False
        finally:
            path = getattr(self, "_config_path", None)
            if path is not None:
                path.unlink(missing_ok=True)

    def parse_output(self, stdout):
        events, result = [], None
        for line in stdout.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("kind") == "DSHEvent":
                events.append(record)
            elif record.get("kind") == "__result__":
                result = record
        out = summarize(events, result, self.model_variant or self.model_name)
        if out["usage"]["requests"]:
            cost, source = price(self.model_variant or self.model_name, out["usage"])
            out["usage"]["cost_usd"] = cost
            out["usage"]["priced_by"] = source
        return out


def _write_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)
