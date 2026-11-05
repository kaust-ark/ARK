# ADR-0014 — DSH runtime migration with evaluation inside the will network

- **Status:** Adapter implementation in progress (production cutover not yet approved)
- **Date:** 2026-11-05
- **Deciders:** Idea2Paper maintainer
- **Related:** [ADR-0003](0003-http-v1-control-plane-boundary.md), [ADR-0009](0009-apptainer-experiment-sandbox.md), [boundary tests](../tests/e2e/README.md)

## Context

The maintainer requested a DSH-based refactor that improves research capability,
not only a different model client. OpenRouter evaluation budget is currently
unavailable. A usable API key exists in the maintainer's **will intranet**, where
the maintainer and a Codex development session are available. mcnode15 cannot
access that network. The endpoint protocol, models, tool-call support, allowed
usage and budget limits remain unverified. Do not assume a host named `will`
is reachable by SSH, or that this mcnode15 session can operate the inner machine.

Starting source: `origin/main` at `40033d2`, production `v0.5.45` as inspected.
Work in the isolated branch `refactor/dsh-runtime`. The unrelated account-reminder
fix `69c5021` lives on another branch and is not included in this baseline.

Current seams worth extending:

- `ark/engines/cli.py`: `AgentCLI`, `get_cli_for_model`, subprocess supervision.
- `ark/engines/sdk_runtime.py` and `sdk_driver.py`: optional OpenHands SDK adapter.
- `ark/engines/__init__.py`: role prompts, context, retry policy, usage accounting.
- `ark/llm_lite.py`: separate utility-model calls. Its `complete()` now honors
  `LLM_BASE_URL` with an optional per-call override. Changing only the main
  agent endpoint would be insufficient for an intranet-only run.
- `ark/pipeline.py`, `execution.py`, `orchestrator/state.py`: task execution,
  step checkpoints and evidence checks.
- `ark/controlplane/`: existing product/runtime boundary.

## Decision

The ARK session on mcnode15 leads the refactor: architecture, runtime adapter,
capability improvements, integration, offline validation and acceptance criteria
stay here. The will session is an on-demand experiment executor. It configures
the local intranet API and runs specific, bounded evaluation tasks against a
fixed source revision when real model calls are necessary. It does not own or
independently redirect the refactor. Transfer source and evaluation summaries only
where permitted. Do not assume intranet source, traces or results may be exported.
mcnode15 remains the production and deployment environment; no cross-network API
proxy is required by this design.

The implementation now includes utility-call endpoint routing and an opt-in DSH
adapter. SDK/runtime `0.2.1rc2` is installed in an isolated development environment.
Validation uses deterministic local API fixtures, not paid model requests. No
production migration or research-quality evaluation has taken place.

### Adapter configuration and current limits

Install the pinned SDK in its own environment, outside the shared platform env:

```sh
python -m venv /path/to/dsh-venv
/path/to/dsh-venv/bin/python -m pip install deepseek-harness-sdk==0.2.1rc2
export ARK_AGENT_RUNTIME=dsh
export ARK_DSH_PYTHON=/path/to/dsh-venv/bin/python
```

The SDK pins the matching runtime wheel. The driver checks both versions before
starting. An unavailable interpreter, missing dependency or invalid configuration
is a terminal error. The default OpenHands runtime remains unchanged.

For custom gateways, set `LLM_BASE_URL` and `ARK_DSH_API` to the protocol served by
that endpoint: `openai-completions`, `openai-responses` or `anthropic-messages`.
An `openai/` model defaults to `openai-completions`. The native `deepseek/` route
uses DSH's DeepSeek Messages adapter; to use an older Chat Completions endpoint,
explicitly set `ARK_DSH_API=openai-completions`. Chat base URLs normally include
`/v1`; the Anthropic client appends `/v1/messages` to its base URL. Select the model
with the existing provider/model notation. Set the corresponding provider API key
or `LLM_API_KEY` locally; unrelated provider/cloud keys are removed from the agent
environment. No key is written into the launch config or command line.

The adapter runs DSH's `sdk` profile with a generated per-call patch
(`ark/engines/dsh_profile.py`). `sdk-minimal` lacked background jobs, spill of
oversized tool output, tool-result pruning and skills. The patch switches off every
row that sends data off the machine (session-log and plugin-inventory upload,
OpenTelemetry export, feedback, DeepSeek account and web search), `web_fetch`,
project `AGENTS.md`/`CLAUDE.md` injection, workflows, goals, plan mode and
sub-agents. The persona names the ARK role. A project's `.claude/skills` is the
only skills root, so ARK's installed skills reach the model for the first time.

Settings: `ARK_DSH_CONTEXT_WINDOW` (default: the litellm window, else 200k) is the
model's real window. `ARK_DSH_MAX_TOKENS` (default 16384) caps each response;
reasoning counts toward it on DeepSeek V4. Compaction triggers at the same history
budget the SDK runtime measured (0.45 of the window, 60k-150k). DSH's catalogued
native models (`deepseek-v4-pro`, `deepseek-flash`) keep a 1M window that the
adapter cannot override, so their threshold is set as a ratio of 1M.
`ARK_DSH_SANDBOX` selects `danger-full-access` (default, production parity),
`workspace-write` or `read-only`; the sandbox needs bwrap on PATH or a Landlock
kernel. `ARK_DSH_RETRIES` (default 3) sets DSH's own retry of rate limits and
5xx. `ARK_DSH_EFFORT` sets the native route's reasoning effort.
`ARK_DSH_PKG_CACHE` holds the ~70 MB the runtime unpacks on first start. Prime it
once per install, because a cold start can exceed short call timeouts. These
settings alone are not a total token or spend budget.

The driver stays attached until the session is idle with no background job open,
so a job started with `run_in_background` finishes and the turn it wakes is the
answer. It stops itself shortly before the outer wall clock and reports open
jobs. Each DSH outcome maps onto a class `run_agent` already has. Rate limits,
5xx, transport and empty responses are transient, and the retry resumes the
same session with a recovery note instead of rerunning the task. The wall clock,
two output-token stops in a row and context overflow count as a used budget:
the work is kept and not retried. Quota is terminal with the provider's message,
and so are authentication and invalid requests. A breaker or host cancellation
keeps the work without a retry. An answer that ends with no text gets one
in-session follow-up: a nudge if an acting role made no tool call, otherwise a
request for a summary.

Each call records its identity before startup under `.dsh/<session-id>/run.json`
in the project, with a private SDK home and patch; the runtime's temporary files
go to `.dsh/tmp`. Project exports are an allowlist and do not include `.dsh/`.
Resume checks workspace, model, endpoint and SDK version. Usage counts every
billed request, attempts the runtime discarded included; unavailable USD cost
is null and the cost ledger marks the known subtotal as incomplete.

Selecting the existing Apptainer option with DSH is still rejected rather than
silently ignored. Read confinement needs the whole runtime inside Apptainer, which
works with the existing SIF but is not wired.

Evidence so far (will network, `ext/deepseek-v4.1-flash`, native route):
E0 passed a fresh turn plus a resumed turn that recalled a codeword held only in
history (6.8k tokens). E1 passed: a background job ran 25 s with no model request
between the turn's end and the wake-up, and a 75 s foreground command was promoted
to a job at the 60 s limit. In the promoted case the model blocked on
`job_output` instead of ending its turn, hence the runtime note now added to every
DSH task. Effort `off` and `high` were indistinguishable on a trivial question.

Run the optional real-SDK, loopback-only tests alongside the standard suite:

```sh
ARK_DSH_TEST_PYTHON=/path/to/dsh-venv/bin/python \
  python -m pytest tests/ -x -q -m 'not network and not gcp and not skypilot'
```

These tests exercise real SDK tools and persisted history with synthetic model
responses. They do not measure model reasoning or research quality. OpenAI
Responses compatibility still needs a dedicated protocol fixture before a real
evaluation uses it.

`scripts/runtime_case.py` runs one role through the real `run_agent` on a fixed
task with a checker the agent cannot satisfy by narration (recomputed metrics,
text outside the edited region byte-identical), for either runtime, and reports
usage in one normalized form.

### Coordination between existing Codex sessions

The will session can send messages to this ARK session through the application's
cross-session tools. The maintainer also relayed will's successful check reading
ARK's latest reply. ARK has no equivalent tool available: a local `codex queue`
attempt cannot resolve will's remote thread. Use will-initiated messages and
explicit reads of ARK's replies, not SSH or an assumed shared filesystem.

- ARK: `01a1a687-c07e-78f3-b5c1-dbec78e70004`, mcnode15.
- will: `01a1a98c-9aa3-7943-95fc-cd57d3dfd7d1`, WSL2 on SH-WILL-XINJIHAO.
- ARK leads implementation and review, defines each experiment and accepts its
  results. Development here continues independently of intranet model access.
- will owns local endpoint configuration and execution of explicitly handed-off
  experiments. It reports results to ARK and reads the response; its ability to
  initiate messages does not make it the project lead.
- Before the first model experiment, will reports the API protocol, usable model
  capabilities, allowed budget and permitted scope of result sharing, without
  secrets or internal addresses. Wait for its acknowledgement before treating a
  handed-off task as accepted. Paid model calls have not started.

Use milestone messages rather than a continuous model polling loop. Session
messages are also data transfers; a working relay does not establish permission
to export intranet code or data. Keep work ownership explicit to avoid overlapping
edits. A source branch or archive must actually be received before will is assumed
to have the integration checkout.

### Model-independent integration

Add DSH as an explicit selectable runtime through the existing engine seam.
Keep the current runtime available for controlled comparisons and rollback.
Use DSH's supported Python SDK and structured events, not its interactive UI.
An explicitly selected but unavailable DSH runtime must report an actionable
failure, rather than silently using OpenHands or a public model endpoint.

Discover and record the actual endpoint protocol, model names and tool-call
support in the intranet environment. Configure them locally. Do not assume an
OpenAI-compatible gateway implements all DeepSeek-specific request fields.
Route utility calls explicitly as well; separate native Deep Research and image
generation integrations must either have available internal equivalents or be
disabled with the reduced capability recorded. Data plots can use matplotlib.
Retrieval requires an available search service or an explicit local corpus;
an LLM endpoint alone is not a literature search service.

Preserve project-level credentials and isolation. DSH homes, workspaces and
session identities must be scoped to the project/run. Do not import the
developer's personal DSH home or inherited unrelated credentials. Pin the
tested SDK/runtime versions and profiles. DSH remains a developer preview.
Review profile defaults: the documented minimal profile has full filesystem
access and a DeepSeek session-log contributor enabled by default. Configure
isolation and disable that contributor for intranet experiments.

### Execution and recovery contract

The adapter must represent success, cancellation, timeout, provider exhaustion,
runtime failure and missing results distinctly. A nonempty final response does
not prove that tools succeeded. Preserve tool errors and artifact evidence.
Keep Stop effective throughout model retries and pending tasks.

Resume an existing durable session only for a continuation of the same task.
Use new sessions for independent work. Record the session identity before work
starts. DSH history restoration does not restore shell/browser processes or
Slurm jobs. Preserve job IDs independently, reconcile their actual states, and
avoid duplicate submission on recovery. API exhaustion must save progress and
surface as an actionable account issue without repeated paid retries.

Account for compaction, retries and cancelled attempts where the provider/runtime
exposes their usage. Mark unavailable cost as unknown rather than zero. A
reported token estimate is not a provider billing receipt.

### Capability improvement after runtime parity

Preserve the current research workflow as the first comparison baseline. After
the DSH adapter passes parity checks, implement one improvement at a time:

1. **Durable task recovery:** continue unfinished work and reconcile experiments
   without redoing completed steps or resubmitting live jobs.
2. **Evidence-driven work selection:** choose actions from unresolved experiment,
   citation, figure or writing requirements. Do not rerun every research phase
   for a prose-only request. Keep reviewer judgment independent.
3. **Asynchronous experiments:** submit, inspect and collect results through
   explicit tools. Wait outside the model loop instead of spending turns on
   repeated job-status polling.

Extend existing state and execution code first. Do not introduce a second
database, generic workflow platform or mandatory multi-agent fan-out. Research
completion remains grounded in actual artifacts, reproducible measurements,
references and user requirements, not only a reviewer score or an agent's claim.

## Implementation and experiment handoff

The maintainer has asked for implementation led by the ARK session. Implement the
runtime and offline tests here. Hand off only the inner environment checks and
bounded real-model experiments to will, rather than moving the entire project.
Each experiment request specifies the source commit, fixture, command, model
requirements, request/token/time limits, expected evidence and pass/fail criteria.
The will reply identifies the revision actually run, exit status, permitted
artifact checks and observed usage, including failures and missing metrics.

1. Read applicable `AGENTS.md` instructions. Use a feature checkout and an isolated
   test DB/projects directory. Never point an intranet development instance at
   the production shared database or enable a second production control loop.
2. Inspect locally configured API access without printing secrets. Establish the
   endpoint protocol, available models, tools/images/streaming capabilities,
   rate limits and a finite evaluation budget with the maintainer. If no numeric
   allowance is available, continue offline work and request one before paid runs.
3. Pin a published DSH SDK/runtime pair or a reproducible upstream revision.
   The upstream source `pyproject.toml` uses a development placeholder version;
   do not turn that placeholder into an installation requirement. Bring approved
   wheels/dependencies inside if the intranet cannot reach package indexes.
4. Implement the selectable runtime and all required model routing. Add offline
   adapter/contract tests covering event mapping, unsuccessful finishes, missing
   results, stop, credentials, session isolation and recovery semantics.
5. First prove orchestration against deterministic fake responses. Existing
   `tests/e2e/fake_openhands.py` and the control-plane boundary tests demonstrate
   this approach; they do not by themselves exercise DSH.
6. Run a small bounded comparison on public/synthetic fixtures with the same
   model, endpoint, task snapshot and token/time allowance on both runtimes.
   If the old runtime cannot use that endpoint, report the confound instead of
   attributing differences entirely to DSH.
7. Only after parity, evaluate the capability improvements above. Report the
   results and outstanding limitations before proposing production cutover.

Do not use the Codex subscription as the experimental agent runtime. Codex is
for implementation and debugging; evaluation calls use the configured intranet
API under its allowance. Start with one evaluation at a time, not a large sweep.

### Minimum evaluation cases

| Case | Evidence required |
|---|---|
| Repair a small experiment | Real execution, passing checks, collected measurements |
| Prose-only paper revision | Requested text changes; existing figures/tables/numbers preserved |
| Interrupt and resume | Saved task/session restored, no duplicate job or completed-phase rerun |
| API balance/limit failure | Bounded stop, intact checkpoint, actionable error; injected offline first |
| Stop during active work | Agent and owned child work cease; no retry after Stop |

Record artifact pass/fail, wall time, requests, tokens including auxiliary
calls, known cost, repeated actions, and duplicate jobs. Predeclare the acceptance
criteria before model runs. A successful adapter smoke test is not evidence of
better research quality. Initial small comparisons establish feasibility, not
statistically reliable superiority.

Standard offline suite (from the repository root, in an isolated environment):

```sh
python -m pytest tests/ -x -q -m 'not network and not gcp and not skypilot'
```

### Transferring work

If the inner machine can access the repository, use its normal Git connection
and the feature branch after it is shared. Otherwise import a source archive of
this commit. `git archive` includes tracked source only, not production DBs,
user projects, `.env` files or untracked credentials. It omits submodule contents;
PaperBanana is unnecessary for the first runtime tests and needs a separate
approved dependency transfer if subsequently enabled.

For an archive checkout without Git history, initialize a local repository and
commit the untouched imported snapshot before editing. Export subsequent commits
with `git format-patch`; they can be applied onto this feature baseline outside.
Avoid replacing the production checkout with an entire copied directory.

Share only source changes and permitted aggregate results. Keep intranet keys,
internal addresses, private data and raw model traces inside the intranet unless
their export is explicitly authorized. Production later uses its own configured
provider credentials; the intranet evaluation key is not a public-service budget.

## Consequences

- Refactoring can proceed without an OpenRouter balance or mcnode15-to-intranet
  connectivity. Real model validation still requires access and an allowance
  inside the network; offline tests cannot establish model effectiveness.
- Production operation, account management and existing research artifacts stay
  independent of the selected runtime. Improvements can be compared separately.
- Two environments require reproducible dependencies and explicit patch/result
  handoffs. Slurm-specific acceptance must also run in an environment that has
  the relevant scheduler; local-only checks cannot claim scheduler parity.

## Alternatives considered

- **Expose the intranet API to mcnode15:** unnecessary network dependency and
  outside the stated intranet-only development constraint.
- **Require more OpenRouter credit before starting:** blocks offline engineering
  despite an available inner development environment.
- **Rewrite the dashboard, workflow and runtime together:** makes regressions
  and model-quality differences difficult to attribute.
- **Use a local/free model as proof of improvement:** useful for smoke testing
  when available, but its different capabilities confound the harness comparison.

## Upstream references checked on 2026-11-05

- [DSH architecture](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)
- [Python SDK lifecycle and resume](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)
- [Model providers and custom protocols](https://deepseek-harness.github.io/deepseek-harness/en/guide/providers)
- [Python profiles and execution defaults](https://deepseek-harness.github.io/deepseek-harness/en/guide/python-sdk)

Upstream links track its default branch. Pin the version actually tested before
implementation depends on its event schema or configuration layout.
