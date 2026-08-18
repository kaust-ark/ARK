# DeepSeek Harness (dsh) as an ARK Agent Runtime

ARK can run its six research agents on **DeepSeek Harness** — DeepSeek's
open-source agent harness — as an alternative to the default OpenHands
runtime. Select it per project with a `dsh/` model string:

```yaml
# config.yaml
model: dsh/deepseek-v4          # provider deepseek-official, model deepseek-v4
deepseek_api_key: "sk-..."      # bridged to DEEPSEEK_API_KEY automatically
```

or at launch: `ark run myproject --model dsh/deepseek-v4-flash`, or pick it as
a custom model string in the dashboard.

**Install the runtime** (Node ≥ 22.19 required):

```bash
npm install -g @deepseek-ai/dsh     # provides the `dsh` binary
ark doctor                          # verifies it when your config selects dsh/…
```

---

## 1. What DeepSeek Harness is

[DeepSeek Harness](https://www.deepseek.com/harness/en/)
([GitHub](https://github.com/deepseek-ai/deepseek-harness), MIT, developer
preview) is DeepSeek's take on the agent = **Model + Harness** split: the model
does the deciding, the harness supplies tools, sandboxing, sessions, and
scheduling. Everything — models, tools, skills, sandboxes, storage, loops, UI —
is a swappable plugin on the [Cordis](https://github.com/cordiverse/cordis)
kernel. A booted "profile" is an ordered stack of YAML patch layers over a
plugin tree; `--patch file.yml` overlays config per invocation.

Pieces relevant to ARK (verified against `@deepseek-ai/dsh` **0.1.0-rc.7**):

| Piece | What it gives us |
|:--|:--|
| `dsh --profile headless "task"` | One-shot agent run: prints the final assistant text on stdout, exit 0 **only** when the turn completed (vs. OpenHands, which exits 0 even on auth failure) |
| Append-only session log | `$DSH_HOME/sessions/<project>/<session-id>/session.jsonl` — every prompt, tool call, result, token count, and error as structured events |
| OS-enforced sandbox | `DSH_PERMISSION_MODE=workspace-write` confines **writes** to the agent's cwd at the kernel level (Landlock on Linux) — ARK's path boundary becomes enforced, not just prompted |
| Fail-closed approvals | In headless runs there is no human answerer, so anything needing approval is **rejected**, never silently allowed and never hung |
| Skills | `SKILL.md` + YAML frontmatter discovered from `<project>/.agents/skills` — the **same format ARK already ships** in `skills/builtin/` |
| Subagents / goals / plan mode | `subagent`, `ralph` (round-based objective loop), `create_goal`/round-driver, plan mode — building blocks for deeper integration (§5) |
| Python SDK | `pip install deepseek-harness-sdk` — sync API over a bundled Node runtime (no system Node), returns final text + full event list |

Scale/maturity check (2026-08-18): ~155k GitHub stars in its first week,
12k+ commits, releases every 1–2 days — and an explicit README warning:
*"THERE WILL BE COMPATIBILITY-BREAKING CHANGES."* Treat every dsh upgrade as a
re-verification event (§6).

## 2. How the ARK engine drives it

`ark/engines/cli.py::DshCLI` implements ARK's standard `AgentCLI` contract
(same seam as `OpenHandsCLI`; `get_cli_for_model()` routes on the `dsh/`
prefix). Per agent call it:

1. **Writes a patch overlay** `<project>/.dsh_home/ark.patch.yml`:
   - `agent-default-model` → the provider/model parsed from the model string
     (`dsh/<model>` = provider `deepseek-official`; `dsh/<provider>/<model>`
     for anything else),
   - `session-persistence-jsonl` → sessions under the project's own
     `.dsh_home/sessions`, `compression: none` so Python can tail/parse
     without zstd,
   - `bash-sandbox` → `timeoutMs` 600000 (dsh's 60 s default is too short for
     experiment installs/compiles; override: `ARK_DSH_BASH_TIMEOUT_MS`).

   A patch entry **replaces** that plugin's whole config (no deep-merge) —
   restate required fields when editing.

2. **Isolates the environment**: `DSH_HOME=<project>/.dsh_home` (sessions,
   settings, credentials stay inside the project — the same philosophy as the
   per-project conda env), strips orchestrator-only credentials
   (`ARK_GITHUB_PAT`, `GITHUB_TOKEN`), sets `DSH_TELEMETRY_MODE=DISABLED`, and
   `DSH_PERMISSION_MODE=workspace-write` (override:
   `ARK_DSH_PERMISSION_MODE`).

3. **Runs** `dsh --profile headless --patch … "<task>"` with cwd = the project
   dir, under ARK's existing subprocess machinery (hard timeout, blocking-
   command watchdog, intervention PATH shims). Binary override: `ARK_DSH_BIN`.

4. **Tails the session log live**: dsh's headless stdout carries only the
   final text, so a background tailer streams `session.jsonl` events into the
   same `on_event` channel OpenHands uses — the live step log, secret
   redaction, `agent_steps.jsonl`, and the circuit breaker (including ABORT
   kills) all keep working. `ark/observability/steps.py::parse_line`
   understands both event dialects (OpenHands `kind`-keyed; dsh
   slash-namespaced `type`).

5. **Parses the outcome**: final text from stdout; error code/detail from the
   session's `turn/end` event (e.g. `AUTH: Authentication Fails …`, surfaced
   verbatim like OpenHands `ConversationErrorEvent`s); token usage folded per
   (turn, step) from `assistant/chunk`/`assistant/message` usage events.
   dsh has **no dollar-cost accounting**, so `cost_usd` is a best-effort
   LiteLLM price-table estimate (the dashboard already labels
   non-provider-billed totals as estimates).

### Runtime comparison

| | OpenHands (default) | DeepSeek Harness (`dsh/…`) |
|:--|:--|:--|
| Model coverage | Any LiteLLM provider | DeepSeek native; other providers via dsh settings (roadmap) |
| Path boundary | Prompt rule only | **Kernel-enforced** (Landlock) + prompt rule |
| Failure signal | Exit 0 even on auth/quota errors; string-matching workarounds | Exit code honest; structured `turn/end` error codes |
| Live events | stdout JSONL | Session-log tail (built into the engine) |
| Cost reporting | Internal LiteLLM cost | Token counts real, USD estimated |
| Escalation | Agent can ask for anything | Approvals **fail closed** headless |
| Maturity | Stable | Developer preview, rc-grade |

## 3. Skills: ARK's library is dsh-native already

dsh discovers project skills from `<project>/.agents/skills` — directories of
`SKILL.md` with `name:`/`description:` YAML frontmatter. That is exactly the
format of ARK's `skills/builtin/` (research-integrity, figure-integrity,
page-adjustment, …), which the pipeline installs into
`<project>/.claude/skills`. The engine bridges the two with one symlink
(`.agents/skills → .claude/skills`), so every ARK skill — including
project-specific ones selected by the Researcher — loads natively into dsh
agents with zero conversion.

This cuts both ways: ARK's skill library is effectively a **paper-writing
skill pack for the dsh ecosystem**, usable by any dsh instance pointed at it.

## 4. Operational notes

- **Keys.** dsh authenticates natively: `deepseek_api_key` in `config.yaml`
  (bridged to `DEEPSEEK_API_KEY`). An OpenRouter key does **not** cover
  `dsh/…` models — the dashboard validates this at launch and never reroutes
  `dsh/` strings through OpenRouter.
- **Env knobs.** `ARK_DSH_BIN` (binary path), `ARK_DSH_PERMISSION_MODE`
  (`read-only` | `workspace-write` | `danger-full-access`),
  `ARK_DSH_BASH_TIMEOUT_MS` (per-command bash cap inside dsh).
- **Error classes.** `AUTH` / `QUOTA` / `CONTEXT_WINDOW_EXCEEDED` abort the
  run fast with the provider's message (dsh's `llm-retry` already retried
  transients internally); `EMPTY_RESPONSE` gets one bounded ARK-side retry.
- **Where things land.** Sessions: `<project>/.dsh_home/sessions/…/
  session.jsonl` (plain JSONL — greppable, replayable). Step log:
  `auto_research/state/agent_steps.jsonl`, same as OpenHands runs.
- **Chat-with-paper** (`ark chat`) still runs on OpenHands regardless of the
  project model — the dsh engine currently covers the six pipeline agents.

## 5. Roadmap: ARK × dsh for AutoResearch

The near-term integration (this change) treats dsh as a drop-in execution
harness. The parts of dsh that map onto ARK's own architecture suggest the
next steps, in rough order of value:

1. **Multi-provider dsh** — write `$DSH_HOME/settings.yaml` provider entries
   (`llm-pi-ai`: Anthropic/OpenAI/OpenRouter/any OpenAI-compatible `baseURL`)
   from ARK config, so `dsh/<provider>/<model>` covers the same catalog as
   OpenHands. Also honour `DEEPSEEK_BASE_URL` for proxies.
2. **Python SDK path** — `deepseek-harness-sdk` (sync, bundled runtime, no
   Node install) can replace the subprocess+tail plumbing with structured
   `RunResult.events` and per-turn `session_id` continuation. Caveat: the
   SDK's bundled composition ships **no sandbox row** — a custom
   `cordis=` composition must re-add `dsh-sandbox-policy` before this path is
   allowed to replace the CLI.
3. **Review loop on dsh goals** — ARK's Dev/Review iteration is exactly dsh's
   `create_goal` + round-driver + `ralph` shape (one fresh child per round
   toward an immutable objective). Running an iteration *inside* one dsh goal
   would give ARK durable, resumable iterations with per-round session logs.
4. **Subagent fan-out** — dsh's `subagent`/`subagent_fork` (with
   `outputSchema` for structured child output) matches ARK's
   researcher→writer/experimenter parallel Execute step; dsh can even bridge
   child turns to Codex or Claude Code (`dsh-subagent-codex`,
   `dsh-subagent-claude-code`) — one harness, mixed agent vendors.
5. **`dsh-plugin-autoresearch`** — package ARK's venue templates, citation
   verification (DBLP/CrossRef/arXiv), figure integrity, and delivery-contract
   checks as a dsh bundle (npm package + `cordis.patch.yml`, installed with
   `dsh plugin add`), making "idea → reviewed paper" a capability any dsh
   user can mount. ARK's control plane stays the multi-tenant brain; the
   plugin is the community-facing distribution.

## 6. Version pinning

Verified against `@deepseek-ai/dsh` **0.1.0-rc.7** (2026-08-17). The project
is pre-1.0 and moves fast. Facts most likely to drift, and to re-verify on
upgrade (`tests/unit/test_dsh_engine.py` encodes all of them):

- headless stdout/exit-code contract and stderr `dsh: <CODE>: <detail>` shape,
- session-log layout, event `type` names, usage field names,
- patch-overlay semantics (whole-config replacement) and plugin ids
  (`agent-default-model`, `session-persistence-jsonl`, `bash-sandbox`),
- `DSH_HOME` / `DSH_PERMISSION_MODE` / `DEEPSEEK_API_KEY` env behaviour,
- skill discovery roots (`.agents/skills`).

Pin a known-good version with `npm install -g @deepseek-ai/dsh@0.1.0-rc.7`
and point `ARK_DSH_BIN` at it if the floating install breaks.
