"""DSH runtime patches for one ARK agent call.

The `sdk` profile carries what an unattended research agent needs and
`sdk-minimal` lacks: background jobs with completion wake-ups, spill of
oversized tool output, tool-result pruning before compaction, and skills.
This module turns it into an Idea2Paper role: everything that phones home,
injects instructions, or lets the model reshape its own harness is switched
off, and the route and compaction policy are made explicit instead of left
to defaults that are wrong for both routes (see compaction() below).
"""
from __future__ import annotations

from pathlib import Path

PROFILE = "sdk"

# Rows switched off for every role. All ids exist in the 0.2.1rc2 sdk profile.
_OFF = (
    # data leaving the machine: session-log and plugin inventory upload with
    # each DeepSeek request; OpenTelemetry export after /feedback
    "session-log-deepseek", "plugin-package-inventory-deepseek",
    "session-telemetry-otel", "command-feedback",
    "deepseek-account", "llm-deepseek-account",
    # web search through DeepSeek's own API, and fetch to any host
    "web-search-deepseek", "web-deepseek-api", "tool-web",
    # a project AGENTS.md / CLAUDE.md silently rewriting every role
    "agent-instructions",
    # orchestration that ARK owns: workflows, goals, plan mode, sub-agents
    "tool-workflow", "workflow-ptc", "plan-mode", "command-goal", "goal",
    "goal-round-driver", "tool-goal", "tool-subagent", "tool-subagent-fork",
    "tool-subagent-control", "tool-subagent-list-agents",
    "tool-working-directory",
)

# DSH's own catalog (llm-deepseek models.ts at rc.2) pins these ids to a
# 1,000,000-token window; `defaultContextWindow` only reaches other ids.
_NATIVE_CATALOG = {"deepseek-v4-pro": 1_000_000, "deepseek-flash": 1_000_000}

_MODES = ("danger-full-access", "workspace-write", "read-only")


def effective_window(native: bool, model_id: str, declared: int) -> int:
    return _NATIVE_CATALOG.get(model_id, declared) if native else declared


def compaction(window: int, max_tokens: int, history_budget: int) -> dict:
    """compaction-basic config that compacts near `history_budget` tokens.

    DSH's defaults are wrong for us on both routes. A catalogued native model
    triggers at 800k (0.8 of 1M), far beyond the 55-95k ARK sessions carry. A
    declared 65,536 window with the default 65,536-token headroom leaves no
    pressure budget at all: proactive compaction is skipped, and pi-ai then
    clamps max_tokens to 1 near the window. The budget is the same one the
    SDK runtime measured (90k good, 24k cost 2.4x), so both runtimes compact
    at the same point.
    """
    headroom = min(16_384, max(1_024, (window - max_tokens) // 8))
    pressure = window - max_tokens - headroom
    if pressure <= 0:
        raise ValueError(f"DSH context window {window} cannot hold max_tokens {max_tokens}")
    threshold = max(1, min(history_budget, int(window * 0.8), pressure))
    return {"thresholdRatio": round(threshold / window, 6), "headroomTokens": headroom,
            "maxTokens": min(8_192, headroom), "retainTokens": threshold // 4}


def role_patch(*, role: str, workdir: Path, provider: str, model_id: str, window: int,
               max_tokens: int, history_budget: int, api: str = "", endpoint: str = "",
               sandbox: str = "danger-full-access", skills_dir: Path | None = None,
               retries: int = 0, image_input: bool = False) -> list:
    if sandbox not in _MODES:
        raise ValueError(f"ARK_DSH_SANDBOX must be one of {', '.join(_MODES)}")
    rows = [{"id": i, "disabled": True} for i in _OFF]
    retry = {"mode": "normal", "maxRetries": retries,
             "backoff": {"initialDelayMs": 2_000, "maxDelayMs": 60_000}}
    native = provider == "deepseek-official"
    if native:
        # A `config:` replaces the row, so restate what the profile set: the
        # stream idle limit falls back to 5 minutes otherwise, and reasoning
        # models can be silent longer than that.
        rows.append({"id": "llm-deepseek", "config": {
            "apiKeyEnv": "ARK_DSH_API_KEY", "defaultContextWindow": window,
            "streamIdleTimeoutMs": 600_000, "retryPolicy": retry}})
    else:
        route = {"api": api, "baseURL": endpoint, "apiKeyEnv": "ARK_DSH_API_KEY",
                 "models": [{"id": model_id, "contextWindow": window, "maxTokens": max_tokens}],
                 "retryPolicy": retry}
        if image_input:
            route["models"][0]["input"] = ["text", "image"]
        # Most chat gateways implement system + max_tokens, not OpenAI's
        # newer developer role and max_completion_tokens fields.
        if api == "openai-completions":
            route["compat"] = {"supportsDeveloperRole": False, "maxTokensField": "max_tokens"}
        rows += [{"id": "llm-deepseek", "disabled": True},
                 {"insert": [{"id": "llm-pi-ai", "name": "@deepseek-ai/dsh-llm-pi-ai",
                              "config": {"providers": {provider: route}}}]}]
    rows += [
        {"id": "compaction-basic",
         "config": compaction(effective_window(native, model_id, window), max_tokens, history_budget)},
        # DSH reads the mode from DSH_PERMISSION_MODE, a name the credential
        # scrub removes, so it is set here. No approval channel exists in the
        # SDK, so an escalation request can only fail; say so up front.
        {"id": "sandbox-policy", "config": {"mode": sandbox, "workspaceRoot": str(workdir)}},
        {"id": "approval", "config": {"policy": "never"}},
        {"id": "tool-fs", "config": {"read": True, "write": True, "edit": True, "readImage": image_input}},
        {"id": "system-prompt", "config": {
            "includeHarnessIdentity": False,
            "personaPrefix": f"You are the Idea2Paper {role} agent, running {{{{model}}}}."}},
        # ARK installs a project's skills here; neither runtime read them
        # before. A fixed catalog keeps the prompt prefix cacheable.
        {"id": "skill-filesystem", "config": {
            "includeDefaultRoots": False, "watch": False,
            "customSkillDirs": [str(skills_dir)] if skills_dir and skills_dir.is_dir() else []}},
    ]
    return rows
