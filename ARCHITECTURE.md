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
│  Phase 1: Research (4-step)                                     │
│  ┌──────────────┐  ┌─────────────┐  ┌─────────┐  ┌──────────┐ │
│  │Deep Research  │─▶│ Initializer │─▶│ Planner │─▶│Experiment│ │
│  │(Gemini)       │  │(bootstrap)  │  │(plan)   │  │(run)     │ │
│  └──────────────┘  └─────────────┘  └─────────┘  └──────────┘ │
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

### Research Phase (4-step pipeline)

| Step | Agent | What Happens |
|:-----|:------|:-------------|
| 1 | Deep Research | Gemini literature survey, background knowledge gathering |
| 2 | Initializer | Bootstrap conda env, install builtin skills, prepare citations |
| 3 | Planner | Generate initial research plan from survey results |
| 4 | Experimenter | Run first round of experiments based on plan |

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

Skills are auto-installed during pipeline bootstrap (Research Phase Step 2).

### 5. Environment Isolation (`webapp/jobs.py`)

Each project gets a sandboxed conda env:

- `provision_project_env()` clones base env to `<project>/.env/`
- `project_env_ready()` checks if env exists
- Orchestrator runs with `HOME=<project_dir>`, `PYTHONNOUSERSITE=1`
- Both CLI (`ark run`) and Web Portal auto-detect and use the project env

### 6. State Management (`webapp/db.py`)

SQLite is the source of truth for project config and status:

- Project creation, config, phase status
- Score history, cost tracking
- CLI and webapp read/write the same DB
- YAML files under `auto_research/state/` are for per-agent runtime state only

## Agent List (9 agents)

| Agent | Role |
|-------|------|
| initializer | Bootstraps project: conda env, skills, citations |
| reviewer | Reviews and scores the paper |
| planner | Analyzes issues, generates action plan (paper & dev modes) |
| experimenter | Designs, runs, and analyzes experiments |
| researcher | Literature search and experimental result analysis |
| writer | Writes/revises paper sections |
| visualizer | Checks and fixes figure/table quality |
| meta_debugger | System-level diagnosis |
| coder | Implements code changes (dev mode) |

## File Structure

```
ARK/
├── ark/
│   ├── orchestrator.py      # Main loop (mixin-based)
│   ├── pipeline.py          # Research phase 4-step pipeline
│   ├── memory.py            # Score tracking, issue dedup, stagnation
│   ├── agents.py            # Agent invocation
│   ├── execution.py         # Agent execution and skill injection
│   ├── cli.py               # CLI commands (ark new/run/status/...)
│   ├── compiler.py          # LaTeX compilation
│   ├── citation.py          # DBLP/CrossRef citation verification
│   ├── deep_research.py     # Gemini Deep Research integration
│   ├── telegram.py          # Telegram notifications + human intervention
│   ├── compute.py           # Slurm/cloud compute backends
│   ├── templates/agents/    # Agent prompt templates
│   │   ├── initializer.prompt
│   │   ├── reviewer.prompt
│   │   ├── planner.prompt
│   │   ├── experimenter.prompt
│   │   ├── researcher.prompt
│   │   ├── writer.prompt
│   │   ├── visualizer.prompt
│   │   └── coder.prompt
│   └── webapp/
│       ├── app.py           # Flask app
│       ├─�� db.py            # SQLite models + state management
│       ├── jobs.py          # Job launch, conda env provisioning
│       ├── routes.py        # API routes + SSE
│       └── static/app.html  # SPA frontend
├── skills/
│   ├── index.json           # Skill registry
│   └── builtin/             # Built-in skills
│       ├── research-integrity/
│       ├── human-intervention/
│       ├── env-isolation/
│       ├── figure-integrity/
│       └── page-adjustment/
├── venue_templates/         # LaTeX templates per venue
├── tests/                   # 115 tests
└── projects/                # Per-project directories (gitignored)
```

## Deprecated

- `events.py` — Event-driven system (replaced by Planner-based decisions)
- Complex Memory tracking (issues, effective_actions, failed_attempts) — simplified
