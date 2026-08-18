# ADR-0014 — DeepSeek Harness as a second agent runtime, selected by model prefix

- **Status:** Implemented
- **Date:** 2026-08-18
- **Deciders:** ARK core
- **Related:** `ark/engines/cli.py` (`DshCLI`), `ark/observability/steps.py`,
  `docs/DEEPSEEK_HARNESS.md`, `tests/unit/test_dsh_engine.py`

## Context

Every heavy ARK agent runs through one seam — `AgentCLI` in `ark/engines/cli.py`
— currently implemented only by `OpenHandsCLI`. OpenHands has two structural
weaknesses we carry workarounds for: it exits 0 even on auth/quota/model
failure (errors must be fished out of the event stream), and ARK's
path-boundary rule is enforced only by prompt text.

DeepSeek Harness (dsh, `@deepseek-ai/dsh`, MIT, developer preview, ~155k stars
in week one) offers a one-shot headless runner with an honest exit code,
structured error codes, an append-only session log carrying every tool call
and token count, an OS-enforced workspace-write sandbox (Landlock), and
fail-closed approval semantics for unattended runs. Its skill format
(`SKILL.md` + YAML frontmatter) is byte-compatible with ARK's `skills/`
library. It is also rc-grade software with an explicit breaking-changes
warning, and its native LLM adapter covers DeepSeek models only (other
providers need extra settings plumbing).

We want to use dsh where it is strong without betting the default path on a
pre-1.0 dependency.

## Decision

We will add **`DshCLI`**, a second `AgentCLI` implementation, selected per
project by a **`dsh/` model-string prefix** (`dsh/deepseek-v4`,
`dsh/<provider>/<model>`) in `get_cli_for_model()`. OpenHands remains the
default for all other model strings.

Specifics:

- One-shot invocation: `dsh --profile headless --patch <generated overlay>
  "<task>"`, cwd = project dir, under the existing subprocess machinery
  (timeout killer, watchdog, intervention shims).
- Per-project isolation: `DSH_HOME=<project>/.dsh_home`; sessions patched to
  plain JSONL inside it; telemetry disabled; permission mode
  `workspace-write` by default.
- Live observability: a session-log tailer feeds the same `on_event` channel
  as OpenHands stdout; `parse_line` gains a dsh event dialect, so step log,
  redaction, and circuit breaker are runtime-agnostic.
- Outcome parsing: final text from stdout; error code/detail from `turn/end`;
  token usage from session events; `cost_usd` is a labeled LiteLLM estimate
  (dsh has no price table).
- Skills bridge: symlink `<project>/.agents/skills → .claude/skills` so the
  installed ARK skill set loads natively in dsh.
- The integration contract is pinned by unit tests against dsh 0.1.0-rc.7
  behaviour (`tests/unit/test_dsh_engine.py`), including a fake-`dsh`
  round trip mirroring `tests/e2e/fake_openhands.py`.

## Consequences

Easier: projects can opt into kernel-enforced path isolation and honest
failure signals today; DeepSeek-native runs stop paying OpenHands quirks;
ARK's skills double as a dsh-ecosystem paper-writing skill pack; deeper dsh
features (goals/round-driver for the review loop, subagents with
`outputSchema`, the Python SDK) have a landed seam to grow from.

Harder / costs: a second runtime to keep verified — dsh is rc-grade and moves
daily, so upgrades require re-running the engine tests and re-checking the
contract list in `docs/DEEPSEEK_HARNESS.md` §6; dsh runs need a native
DeepSeek key (no OpenRouter rerouting — enforced at the dashboard); cost
totals for dsh runs are estimates until dsh grows billing introspection;
`ark chat` stays OpenHands-only for now.

## Alternatives considered

- **Python SDK (`deepseek-harness-sdk`) instead of the CLI.** Cleaner events
  (structured `RunResult`), no Node on PATH (bundled runtime) — but its
  default composition ships **no sandbox row** (effectively
  danger-full-access), losing dsh's main advantage, and it bypasses the
  battle-tested `AgentCLI` subprocess path (watchdog, intervention shims).
  Revisit once we compose a sandboxed `cordis=` profile for it (roadmap §5.2).
- **A `runtime:` config knob instead of the model prefix.** New plumbing
  through config, CLI `--model`, the dashboard picker, and restart paths —
  whereas the model string already travels all of them end-to-end. The prefix
  also reads naturally next to `anthropic/…`, `openrouter/…`.
- **Replacing OpenHands outright.** dsh is pre-1.0 with declared breaking
  changes and DeepSeek-only native model coverage; the default path stays on
  the stable runtime.
- **Wrapping dsh's web/API server.** The web profile is interactive-first
  (refuses `0.0.0.0`, approval UI in the loop) — wrong shape for a headless
  orchestrator; the headless bundle is purpose-built for exactly our call
  pattern.
