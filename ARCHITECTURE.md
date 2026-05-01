# ARK Architecture

## Design Principles

**Core idea**: Trust the AI's judgment; code handles execution and guardrails only.

- **DB as source of truth** &mdash; project config and status live in SQLite; YAML is used only for per-agent runtime state
- **Per-project isolation** &mdash; each project gets its own conda env, sandboxed HOME, and `PYTHONNOUSERSITE=1`
- **Skills over hard-coded rules** &mdash; modular instruction sets (skills) are loaded at runtime to enforce best practices

## Pipeline Overview

ARK runs three phases in sequence:

```
┌─────────────────────────────────────────────────────────────────┐
│                        ARK Pipeline                             │
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
| 0 | — | **Setup**: provision per-project conda env (clones ark-base — research stack only, no ARK code; orchestrator's ARK is injected via `PYTHONPATH`) |
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

ARK supports multiple compute backends for running experiments:

- **Local**: Runs experiments directly on the host machine.
- **Slurm**: Submits jobs to HPC clusters using `sbatch`.
- **Cloud**: Provisions instances on **AWS**, **GCP**, or **Azure**.
- **Custom**: Extensible backend for specialized environments.

Cloud backends handle the full lifecycle: provisioning, code transfer (rsync), setup, execution, result collection, and teardown.

### 7. AI Figure Generation (`ark/nano_banana.py`)

**Nano Banana** is a Gemini-powered system for generating high-quality scientific figures:

- **Planner**: Designs a detailed visual specification based on paper context.
- **Stylist**: Refines the specification to match academic publication aesthetics.
- **Visualizer**: Generates the image using Gemini image generation models.
- **Critic**: Evaluates the figure and provides feedback for iterative improvement.

## Agent List (6 agents)

| Agent | Role |
|-------|------|
| researcher | Analyzes proposal → `idea.md`; literature survey; specializes agent prompts and selects skills |
| reviewer | Reviews and scores the paper; checks experiment alignment against proposal |
| planner | Analyzes issues, generates action plan (paper & dev modes); verifies experiment alignment |
| writer | Writes/revises paper sections with DBLP-verified citations |
| experimenter | Designs, runs, and analyzes experiments; supports Slurm and Cloud backends |
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
│   ├── compute/             # Compute backends (Local, Slurm, AWS, GCP, Azure)
│   ├── engines/             # Agent orchestration and backend runtimes (Claude, Gemini)
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
