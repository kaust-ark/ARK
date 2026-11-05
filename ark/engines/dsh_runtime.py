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
from ark.llm_lite import resolve_key


class DSHRuntime(AgentCLI):
    def __init__(self, model_name, model_variant=None, *, resume_session_id=None):
        super().__init__(model_name, model_variant)
        self.resume_session_id = resume_session_id
        self.session_id = None

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
        context = int(os.environ.get("ARK_DSH_CONTEXT_WINDOW", "65536"))
        max_tokens = int(os.environ.get("ARK_DSH_MAX_TOKENS", "8192"))
        if not 0 < max_tokens < context:
            raise ValueError("DSH requires 0 < ARK_DSH_MAX_TOKENS < ARK_DSH_CONTEXT_WINDOW")

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
        else:
            root.mkdir(parents=True, mode=0o700, exist_ok=False)
            _write_private(manifest, {"session_id": sid, "workdir": str(workdir),
                                     "model": model, "endpoint": endpoint, "protocol": protocol,
                                     "sdk_version": SDK_VERSION})
        self.session_id = sid
        patch = [
            {"id": "session-log-deepseek", "disabled": True},
            {"id": "plugin-package-inventory-deepseek", "disabled": True},
            {"id": "tool-fs", "config": {"read": True, "readImage": True, "write": True, "edit": True}},
        ]
        provider = "deepseek-official"
        if custom:
            provider = "ark-gateway"
            route = {"api": protocol, "baseURL": endpoint, "apiKeyEnv": "ARK_DSH_API_KEY",
                     "models": [{"id": model_id, "contextWindow": context, "maxTokens": max_tokens}],
                     "retryPolicy": {"mode": "normal", "maxRetries": 0}}
            # Most chat gateways implement system + max_tokens, not OpenAI's
            # newer developer role and max_completion_tokens fields.
            if protocol == "openai-completions":
                route["compat"] = {"supportsDeveloperRole": False, "maxTokensField": "max_tokens"}
            patch.extend([
                {"id": "llm-deepseek", "disabled": True},
                {"insert": [{"id": "llm-pi-ai", "name": "@deepseek-ai/dsh-llm-pi-ai",
                             "config": {"providers": {provider: route}}}]},
            ])
        else:
            patch.append({"id": "llm-deepseek", "config": {
                "apiKeyEnv": "ARK_DSH_API_KEY", "defaultContextWindow": context,
                "retryPolicy": {"mode": "normal", "maxRetries": 0}}})
        _write_private(root / "runtime.patch.yml", patch)
        config = {"session_id": sid, "resume": bool(self.resume_session_id),
                  "model": model, "model_id": model_id, "provider": provider,
                  "workdir": str(workdir), "dsh_home": str(root / "home"),
                  "patch": str(root / "runtime.patch.yml"), "base_url": endpoint,
                  "max_tokens": max_tokens, "task": f"[SYSTEM RULE] {path_boundary}\n\n{prompt}"}
        self._config_path = root / "request.json"
        _write_private(self._config_path, config)
        return [os.environ.get("ARK_DSH_PYTHON") or sys.executable,
                str(Path(__file__).with_name("dsh_driver.py")), str(self._config_path)]

    def execute(self, prompt, path_boundary, code_dir, timeout, log_fn=None, on_event=None, env=None):
        aborted = False

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
            if timed_out or rc:
                # A partial assistant message cannot turn an interrupted run
                # into success. Events before the interruption retain usage.
                code = "DSHTimeout" if timed_out else "DSHCancelled" if aborted else "DSHProcessError"
                if timed_out or aborted or not self.parse_output(stdout).get("error_code") or "__result__" not in stdout:
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
        return summarize(events, result, self.model_variant or self.model_name)


def _write_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)
