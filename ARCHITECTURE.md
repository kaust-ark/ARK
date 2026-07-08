# idea2paper Architecture

## Design Principles

**Core idea**: Trust the AI's judgment; code handles execution and guardrails only.

- **DB as source of truth** &mdash; project config and status live in SQLite; YAML is used only for per-agent runtime state
- **Per-project isolation** &mdash; each project gets its own conda env, sandboxed HOME, and `PYTHONNOUSERSITE=1`
- **Skills over hard-coded rules** &mdash; modular instruction sets (skills) are loaded at runtime to enforce best practices

## Pipeline Overview

idea2paper runs three phases in sequence:

```
┌─────────────────────────────────────────────────────────────────┐
│                        idea2paper Pipeline                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Phase 1: Research (5-step)                                     │
│  ┌────────┐  ┌──────────┐  ┌─────────────┐  ┌──────────┐  ┌──────────┐ │
│  │ Setup  │─▶│ Analyze  │─▶│Deep Research│─▶│Specializ.│─▶│Bootstrap │ │
│  │(conda) │  │ Proposal │  │  (Gemini)   │  │(researcher│  │(skills + │ │
│  │        │  │(researcher│  │             │  │           │  │citations)│ │
│  └────────┘  └──────────┘  └─────────────┘  └──────────┘  └──────────┘ │
│                                                                 │
│  Phase 2: Dev                                                   │
│  ┌───────────────────────────────────────────────────────┐     │
│  │  plan → experiment on Slurm → analyze → write draft   │     │
│  └───────────────────────────────────────────────────────┘     │
│                                                                 │
│  Phase 3: Review (iterative loop)                               │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌��─────────┐      │
│  │ Compile  │─▶│ Review   │─▶│ Planner  │─▶│ Execute  │──┐   │
│  │ LaTeX    │  │ Score    │  │ Decide   │  │ Run      │  │   │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘  │   │
│       ▲                                                   │   │
│       └──── Validate ◀────────────────────────────────────┘   │
���             (recompile)                                        │
│                                                                 │
│  Loop until score ≥ threshold or human intervention             │
└─────────────────────────────────────────────────────────────────┘
```

### Research Phase (5-step pipeline)

| Step | Agent/Tool | What Happens |
|:-----|:-----------|:-------------|
| 0 | — | **Setup**: provision per-project conda env (clones ark-base — research stack only, no idea2paper code; orchestrator's idea2paper is injected via `PYTHONPATH`) |
| 1 | Researcher | **Analyze Proposal**: read uploaded PDF or idea → write `idea.md` (summary, methodology, systems); output Deep Research query; parse and commit paper title |
| 2 | Gemini | **Deep Research**: literature survey → `deep_research.md`; PDF sent to user via Telegram |
| 3 | Researcher | **Specialization**: generate `project_context.md` (web-verified); specialize agent prompt templates for the project; select relevant skills (0–5) |
| 4 | — | **Bootstrap**: install builtin skills; bootstrap citations → `references.bib` |

### Review Loop

Each iteration runs 5 steps: Compile → Review → Plan → Execute → Validate.

The Planner outputs structured YAML action plans:

```yaml
actions:
  - agent: experimenter
    task: "Run perplexity validation experiment"
    priority: 1
  - agent: writer
    task: "Update Section 4.2"
    priority: 2
```

## Core Components

### 1. Memory System (`memory.py`)

Tracks scores, detects stagnation, and prevents repetitive failures:

```python
class SimpleMemory:
    scores: List[float]       # Score history (last 20)
    best_score: float         # Historical best
    stagnation_count: int     # Consecutive stagnation count

    def record_score(score)   # Record a score
    def is_stagnating()       # Stagnation detection
    def get_context()         # Get context (Goal Anchor + score trends)
```

Additional features:
- **Issue tracking**: Content-based dedup — counts how many times each issue reappears across iterations
- **Repair validation**: Verifies that attempted fixes actually resolved the issue
- **Strategy escalation**: Automatically bans ineffective methods and suggests alternatives
- **Meta-debugging**: Triggers diagnostic when the system is stuck

### 2. Goal Anchor

Every agent invocation includes a constant "Goal Anchor" that describes the project's core objectives. This prevents agents from drifting off-topic over many iterations.

### 3. Orchestrator (`orchestrator.py`)

The Orchestrator uses a mixin-based design to compose specialized functionalities:

```python
class Orchestrator(AgentMixin, CompilerMixin, ExecutionMixin, PipelineMixin):
    # AgentMixin: agent invocation and cost tracking
    # CompilerMixin: LaTeX compilation and PDF management
    # ExecutionMixin: skill injection and command execution
    # PipelineMixin: high-level research, dev, and review loops
```

- **Dispatches** to the correct phase based on the project's current mode.
- **Syncs** status, scores, and progress to the SQLite database after each step.
- **Handles** bi-directional Telegram communication and human-in-the-loop decisions.

### 4. Skills System (`skills/`)

Modular instruction sets loaded at runtime to guide agent behavior:

| Skill | Purpose |
|:------|:--------|
| **research-integrity** | Anti-simulation: agents must run real experiments |
| **human-intervention** | Escalation protocol via Telegram for blockers |
| **env-isolation** | Per-project environment boundaries and security |
| **figure-integrity** | Validates that figures match actual experimental data |
| **page-adjustment** | Content density control to fit within venue page limits |

Skills are auto-installed during the Pipeline Bootstrap (Research Phase Step 4).

### 5. Environment Isolation (`website/dashboard/jobs.py`)

Each project gets a sandboxed conda environment:

- `provision_project_env()` clones the base environment to `<project>/.env/`
- `project_env_ready()` checks if the environment exists
- The Orchestrator runs with `HOME=<project_dir>` and `PYTHONNOUSERSITE=1`
- Both the CLI (`ark run`) and the Dashboard auto-detect and use the project-local environment.

### 6. Compute Backends (`ark/compute/`)

idea2paper supports multiple compute backends for running experiments:

- **Local**: Runs experiments directly on the host machine.
- **Slurm**: Submits jobs to HPC clusters using `sbatch`.
- **SkyPilot**: Provisions clusters across **AWS**, **GCP**, **Azure**, or **Kubernetes** from one abstraction, with spot instances, retries, and autostop teardown built in.
- **Custom**: Extensible backend for specialized environments.

SkyPilot handles the full lifecycle: provisioning, code sync (SkyPilot `workdir`/`file_mounts`), setup, execution, result collection, and **autostop** teardown.

### 7. AI Figure Generation (`ark/nano_banana.py`)

**Nano Banana** is a Gemini-powered system for generating high-quality scientific figures:

- **Planner**: Designs a detailed visual specification based on paper context.
- **Stylist**: Refines the specification to match academic publication aesthetics.
- **Visualizer**: Generates the image using Gemini image generation models.
- **Critic**: Evaluates the figure and provides feedback for iterative improvement.

### 8. Intervention & Observability (`ark/intervention/`, `ark/observability/`)

Autonomous runs can take consequential actions — delete files, launch many
compute jobs, request/expose credentials, push or exfiltrate data, overspend.
This subsystem makes each step **visible** and the high-stakes ones **pause for
a human** (over Telegram), without touching the webapp (an `ApprovalChannel`
seam leaves room for a webapp approver later).

**Observability** (`ark/observability/steps.py`). OpenHands' `--json` mode
already streams a JSONL event per action. `OpenHandsCLI.execute()` now reads
that stream line-by-line (instead of one blocking `communicate()`), so:

- every bash command / file edit / observation / message becomes a typed
  `StepEvent`, written to `auto_research/state/agent_steps.jsonl` (full fidelity)
  and emitted as a one-line human-readable step (gated by `log_verbosity`);
- a `Redactor` scrubs secret values (`NAME=value` and registered API keys)
  before anything is logged;
- the CLI `ark monitor` and the webapp's log tail show live progress for free.

**Intervention** — three cooperating layers:

| Layer | Where | What |
|:------|:------|:-----|
| **Policy** (`policy.py`) | pure logic | Classify a command/action → (category, severity) → decision (`allow`/`notify`/`ask`/`deny`) under an autonomy level. Categories: `destructive_fs`, `bulk_compute`, `credentials`, `data_exfil`, `spend`. |
| **B — capability wrappers** (`wrappers.py`) | agent sandbox | Shadow-PATH shims for `rm`/`sbatch`/`gcloud`/`scp`/… sit first on PATH; they phone home to the gate **before** the real binary runs. A dumb shim + a smart `ApprovalWatcher` keeps all policy in one place. (Agent `git` is intentionally not wrapped — too noisy.) |
| **C — circuit breaker** (`manager.py`) | event stream | Backstop: watches the streamed actions and, on a wrapper **bypass** (absolute-path call to a risky binary), runs it through the gate and aborts the agent on denial. |

The **gate** (`gate.py`) turns an `ask` into a human verdict over a pluggable
`ApprovalChannel` (Telegram today), with **approval memory** (remember this
command shape / whole category), an **audit trail**
(`intervention_audit.jsonl`), and a **safe default of deny on timeout**. It
**fails open**: with no channel configured it auto-allows and logs, so existing
projects and CI never block.

The orchestrator's own autonomous actions are gated too — **cloud provisioning**
(`compute/skypilot.py`), **git push** (`core.git_commit`), and a
**cumulative-spend** threshold (after each agent's cost is tallied) — but
human-triggered operations (`ark clear`, delete, stop) are not.

`InterventionManager` (`manager.py`) wires it all together and is attached to the
orchestrator as `self._intervention`; `run_agent` consults it for the sandbox env
and the per-line event handler. Configure via the `intervention:` block (see
`config.example.yaml`).

## Agent List (6 agents)

| Agent | Role |
|-------|------|
| researcher | Analyzes proposal → `idea.md`; literature survey; specializes agent prompts and selects skills |
| reviewer | Reviews and scores the paper; checks experiment alignment against proposal |
| planner | Analyzes issues, generates action plan (paper & dev modes); verifies experiment alignment |
| writer | Writes/revises paper sections with DBLP-verified citations |
| experimenter | Designs, runs, and analyzes experiments; supports Slurm and SkyPilot backends |
| coder | Implements code changes (dev mode) |

## File Structure

```
ARK/
├── ark/
│   ├── orchestrator.py      # Main loop (mixin-based)
│   ├── pipeline.py          # Phase 1 (Research) and Phase 2 (Dev/Review) logic
│   ├── memory.py            # Score tracking, issue dedup, stagnation detection
│   ├── execution.py         # Agent execution and skill injection
│   ├── cli.py               # CLI commands (ark new/run/status/access/...)
│   ├── compute/             # Compute backends (Local, Slurm, SkyPilot, Custom)
│   ├── engines/             # Agent orchestration; runs every agent through OpenHands (any LiteLLM model)
│   ├── intervention/        # Pre-action guardrails: policy, approval gate, capability wrappers, watcher
│   ├── observability/       # Stream OpenHands events → redacted live step log
│   ├── llm_lite.py          # Lightweight LiteLLM helper for non-agent text calls (titles, summaries, bot)
│   ├── orchestrator/        # State and Workspace management
│   ├── telegram/            # Telegram notifications + bidirectional bot
│   ├── website/             # Dashboard and Homepage (FastAPI + SQLite)
│   ├── nano_banana.py       # AI figure generation pipeline
│   ├── citation.py          # DBLP/CrossRef citation verification
│   ├── deep_research.py     # Gemini Deep Research integration
│   ├── compiler.py          # LaTeX compilation logic
│   └── templates/agents/    # Agent prompt templates
├── website/                 # Web interface
│   ├── dashboard/           # FastAPI backend + SQLite DB
│   └── homepage/            # Static landing page
├── skills/                  # Modular instruction sets
│   ├── index.json           # Skill registry
│   ├── builtin/             # Built-in skills (auto-installed)
│   └── library/             # Domain-specific skills (selected by researcher)
├── venue_templates/         # LaTeX templates per conference
├── tests/                   # Comprehensive test suite
└── projects/                # Per-project directories (gitignored)
```

## Deprecated / Removed

- `events.py` — Event-driven system (replaced by Planner-based decisions)
- Complex Memory tracking (issues, effective_actions, failed_attempts) — simplified
- `initializer` agent — merged into `researcher` (Analyze Proposal step)
- `visualizer` agent — removed (dead code, never called in pipeline)
- `meta_debugger` agent — removed (could diagnose but not act; replaced by pipeline-level stall detection)
- `ark/webapp/` Python module — moved to `website/dashboard/`
