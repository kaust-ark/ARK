# ARK Architecture

## Design Principles

**Core idea**: Trust the AI's judgment; code handles execution and guardrails only.

## Pipeline Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Simplified Pipeline                       │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│   ┌──────────┐    ┌──────────┐    ┌──────────┐              │
│   │ Reviewer │───▶│ Planner  │───▶│ Execute  │              │
│   │  Review   │    │  Decide   │    │  Run      │              │
│   └──────────┘    └────┬─────┘    └──────────┘              │
│                        │                                     │
│                        ▼                                     │
│              Planner outputs YAML:                           │
│              actions:                                        │
│                - agent: experimenter                         │
│                  task: "..."                                 │
│                - agent: writer                               │
│                  task: "..."                                 │
│                                                              │
│   ┌──────────────────────────────────────────┐              │
│   │           Memory (minimal)               │              │
│   │  - scores: [7.0, 7.2, 7.5, ...]          │              │
│   │  - is_stagnating() → bool                │              │
│   │  - GOAL_ANCHOR (constant)                │              │
│   └──────────────────────────────────────────┘              │
│                                                              │
└─────────────────────────────────────────────────────────────┘
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
- **Issue tracking**: Counts how many times each issue reappears across iterations
- **Repair validation**: Verifies that attempted fixes actually resolved the issue
- **Strategy escalation**: Automatically bans ineffective methods and suggests alternatives
- **Meta-debugging**: Triggers diagnostic when the system is stuck

### 2. Goal Anchor

Every agent invocation includes a constant "Goal Anchor" that describes the project's core objectives. This prevents agents from drifting off-topic over many iterations.

The Goal Anchor is project-specific and should be configured per project.

### 3. Planner Agent

The **core decision-maker**. Outputs a structured action plan:

```yaml
actions:
  - agent: experimenter
    task: "Run perplexity validation experiment"
    priority: 1
  - agent: writer
    task: "Update Section 4.2"
    priority: 2
```

### 4. Orchestrator (`orchestrator.py`)

Minimal control flow:

```python
def run_paper_iteration():
    # 1. Review
    review = run_agent("reviewer")
    score = parse_score(review)
    memory.record_score(score)

    # 2. Stagnation detection
    if memory.is_stagnating():
        send_notification("Human intervention needed")

    # 3. Planner decides + execute
    run_planner_cycle(review)

    # 4. Visualize + commit
    run_figure_phase()
    compile_latex()
    git_commit()
```

## Agent List (8 agents)

| Agent | Role |
|-------|------|
| reviewer | Reviews and scores the paper |
| planner | Analyzes issues, generates action plan (paper & dev modes) |
| experimenter | Designs, runs, and analyzes experiments |
| researcher | Literature search and experimental result analysis |
| writer | Writes/revises paper sections |
| visualizer | Checks and fixes figure/table quality |
| meta_debugger | System-level diagnosis |
| coder | Implements code changes (dev mode) |

## Deprecated

- `events.py` — Event-driven system (replaced by Planner-based decisions)
- Complex Memory tracking (issues, effective_actions, failed_attempts) — simplified

## File Structure

```
ARK/
├── orchestrator.py    # Main loop
├── memory.py          # Memory system
├── agents/            # Agent prompt templates
│   ├── reviewer.prompt
│   ├── planner.prompt
│   ├── experimenter.prompt
│   ├── researcher.prompt
│   ├── writer.prompt
│   ├── visualizer.prompt
│   ├── meta_debugger.prompt
│   └── coder.prompt
├── state/             # Runtime state (gitignored)
│   ├── action_plan.yaml
│   ├── latest_review.md
│   ├── findings.yaml
│   └── memory.yaml
└── logs/              # Execution logs (gitignored)
```
