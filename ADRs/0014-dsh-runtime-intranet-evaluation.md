# ADR-0014 — DSH runtime migration with evaluation inside the will network

- **Status:** Proposed (implementation requested; production cutover not yet approved)
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

Develop the migration in isolated feature checkouts, with integration and offline
regression testing on mcnode15 and intranet API adaptation and model evaluation
inside will. Transfer source changes and reviewable evaluation summaries only
where permitted. Do not assume intranet source, traces or results may be exported.
mcnode15 remains the production and deployment environment; no cross-network API
proxy is required by this design.

The current implementation covers utility-call endpoint routing and offline
gateway tests. It does **not** yet add a DSH adapter, migrate production, install
DSH, or run model evaluations.

### Coordination between existing Codex sessions

The will session can send messages to this ARK session through the application's
cross-session tools. The maintainer also relayed will's successful check reading
ARK's latest reply. ARK has no equivalent tool available: a local `codex queue`
attempt cannot resolve will's remote thread. Use will-initiated messages and
explicit reads of ARK's replies, not SSH or an assumed shared filesystem.

- ARK: `01a1a687-c07e-78f3-b5c1-dbec78e70004`, mcnode15.
- will: `01a1a98c-9aa3-7943-95fc-cd57d3dfd7d1`, WSL2 on SH-WILL-XINJIHAO.
- ARK owns integration, utility routing and offline regression checks.
- will's first task is to report the API protocol, usable model capabilities,
  allowed budget and permitted scope of result sharing, without secrets or
  internal addresses. Wait for its acknowledgement before treating this task as
  accepted. Paid model calls have not started.

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

## Execution handoff for the intranet checkout

The maintainer has asked for implementation. Continue the work from this record;
do not stop at producing another architecture proposal.

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
