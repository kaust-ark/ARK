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
| 0 | — | **Setup**: provision per-project conda env (clones ark-base; idempotent) |
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

Mixin-based design with 5 mixins:

```python
class Orchestrator(ResearchMixin, DevMixin, ReviewMixin, FigureMixin, BaseMixin):
    # Dispatches to the correct phase based on mode
    # Syncs status to DB after each step
    # Handles Telegram notifications
```

### 4. Skills System (`skills/`)

Modular instruction sets loaded at runtime:

| Skill | Purpose |
|:------|:--------|
| **research-integrity** | Anti-simulation: agents must run real experiments |
| **human-intervention** | Escalation protocol via Telegram |
| **env-isolation** | Per-project environment boundaries |
| **figure-integrity** | Validates figures match actual data |
| **page-adjustment** | Content density control within page limits |

Skills are auto-installed during pipeline bootstrap (Research Phase Step 4).

### 5. Environment Isolation (`website/dashboard/jobs.py`)

Each project gets a sandboxed conda env:

- `provision_project_env()` clones base env to `<project>/.env/`
- `project_env_ready()` checks if env exists
- Orchestrator runs with `HOME=<project_dir>`, `PYTHONNOUSERSITE=1`
- Both CLI (`ark run`) and Dashboard auto-detect and use the project env
- Pipeline bootstraps the env as **Step 0** of the Research Phase (hard fail if provisioning fails)

### 6. State Management (`website/dashboard/db.py`)

SQLite is the source of truth for project config and status:

- Project creation, config, phase status
- Score history, cost tracking
- CLI and webapp read/write the same DB
- YAML files under `auto_research/state/` are for per-agent runtime state only

## Agent List (6 agents)

| Agent | Role |
|-------|------|
| researcher | Analyzes proposal → `idea.md`; literature survey; specializes agent prompts and selects skills |
| reviewer | Reviews and scores the paper; checks experiment alignment against proposal |
| planner | Analyzes issues, generates action plan (paper & dev modes); verifies experiment alignment |
| writer | Writes/revises paper sections with DBLP-verified citations |
| experimenter | Designs, runs, and analyzes experiments; multi-provider API fallback |
| coder | Implements code changes (dev mode) |

## File Structure

```
ARK/
├── ark/
│   ├── orchestrator.py      # Main loop (mixin-based)
│   ├── pipeline.py          # Research phase 5-step pipeline
│   ├── memory.py            # Score tracking, issue dedup, stagnation
│   ├── agents.py            # Agent invocation (Claude + Gemini CLI)
│   ├── execution.py         # Agent execution and skill injection
│   ├── cli.py               # CLI commands (ark new/run/status/access/...)
│   ├── access.py            # Cloudflare Access allowlist management
│   ├── compiler.py          # LaTeX compilation
│   ├── citation.py          # DBLP/CrossRef citation verification
│   ├── deep_research.py     # Gemini Deep Research integration
│   ├── telegram.py          # Telegram notifications + human intervention
│   ├── compute.py           # Slurm/cloud compute backends
│   └── templates/agents/    # Agent prompt templates
│       ├── researcher.prompt
│       ├── reviewer.prompt
│       ├── planner.prompt
│       ├── experimenter.prompt
│       ├── writer.prompt
│       └── coder.prompt
├── website/
│   ├── dashboard/           # FastAPI dashboard (served at /dashboard)
│   │   ├── app.py           # FastAPI app + lifespan (also mounts homepage)
│   │   ├── db.py            # SQLite models + state management
│   │   ├── jobs.py          # Job launch, conda env provisioning
│   │   ├── routes.py        # API routes + SSE
│   │   ├── constants.py     # DASHBOARD_PREFIX and shared constants
│   │   └── static/          # SPA frontend assets
│   └── homepage/            # Static homepage files (served at /)
├── skills/
│   ├── index.json           # Skill registry
│   └── builtin/             # Built-in skills
│       ├── research-integrity/
│       ├── human-intervention/
│       ├── env-isolation/
│       ├── figure-integrity/
│       └── page-adjustment/
├── venue_templates/         # LaTeX templates per venue
├── tests/                   # 114 tests
└── projects/                # Per-project directories (gitignored)
```

## Deprecated / Removed

- `events.py` — Event-driven system (replaced by Planner-based decisions)
- Complex Memory tracking (issues, effective_actions, failed_attempts) — simplified
- `initializer` agent — merged into `researcher` (Analyze Proposal step)
- `visualizer` agent — removed (dead code, never called in pipeline)
- `meta_debugger` agent — removed (could diagnose but not act; replaced by pipeline-level stall detection)
- `ark/webapp/` Python module — moved to `website/dashboard/`
