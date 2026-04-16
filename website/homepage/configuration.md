# Configuration

## Project Config (`config.yaml`)

Each project has a `config.yaml` created by `ark new`. Here's a complete reference:

```yaml
# ── Core ──────────────────────────────────────────
title: "My Paper Title"
code_dir: /path/to/project              # Root directory for code + paper
latex_dir: /path/to/project/paper       # LaTeX source directory
figures_dir: /path/to/project/paper/figures
scripts_dir: /path/to/project/code      # Experiment scripts
venue_format: neurips                    # See Supported Venues below
max_pages: 9

# ── Thresholds ────────────────────────────────────
paper_accept_threshold: 7.5             # Score (1-10) to trigger acceptance
max_iterations: 20                       # Safety limit

# ── Research ──────────────────────────────────────
goal_anchor: "Achieve X on benchmark Y"
research_idea: "Start from A, apply B, measure C"
initial_paper_writing_prompt: "..."      # Detailed first-iteration instructions

# ── Compute ───────────────────────────────────────
compute_backend: slurm                   # slurm | local | aws | gcp | azure | custom
use_slurm: true                          # Legacy flag (still supported)
conda_env: my-env
slurm_partition: gpu
slurm_account: my-lab

# ── AI Model ──────────────────────────────────────
model: claude                            # claude | gemini | codex
use_nano_banana: true                    # AI figure generation
nano_banana_model: flash                 # flash (free) | pro ($0.13/img)

# ── Telegram ──────────────────────────────────────
telegram_bot_token: "bot123:ABC..."
telegram_chat_id: "12345678"

# ── Authors ───────────────────────────────────────
authors:
  - "First Author"
  - "Second Author"
```

## Global Config (`~/.ark/`)

```
~/.ark/
├── config.yaml          # Global Gemini API key
└── telegram.yaml        # Global Telegram bot credentials (shared by all projects)
```

## Supported Venues

ARK auto-configures page geometry, font sizes, and figure dimensions:

| Venue | Format Key | Columns | Font | Column Width |
|-------|-----------|---------|------|-------------|
| **NeurIPS** | `neurips` | 1 | 10pt | 5.5 in |
| **ICML** | `icml` | 2 | 10pt | 3.25 in |
| **ICLR** | `neurips` | 1 | 10pt | 5.5 in |
| **AAAI** | `aaai` | 2 | 10pt | 3.3 in |
| **ACL / EMNLP** | `acmart-sigplan` | 2 | 10pt | 3.333 in |
| **EuroMLSys / PLDI** | `acmart-sigplan` | 2 | 10pt | 3.333 in |
| **ASPLOS / SOSP** | `acmart-large` | 2 | 9pt | 3.333 in |
| **CVPR / ECCV** | `ieee` | 2 | 10pt | 3.5 in |
| **OSDI / NSDI** | `ieee` | 2 | 10pt | 3.5 in |
| **INFOCOM** | `ieee` | 2 | 10pt | 3.5 in |
| **LNCS** (Springer) | `lncs` | 1 | 10pt | 4.8 in |

Custom venue? Add to `VENUE_PRESETS` in `ark/latex_geometry.py` or use `acmart-sigplan` as a fallback.

## Compute Backends

| Backend | Config | Requirements |
|---------|--------|-------------|
| **Slurm** | `compute_backend: slurm` | `srun`, `sbatch`, `squeue` on PATH |
| **Local** | `compute_backend: local` | Default — runs experiments in subprocess |
| **AWS** | `compute_backend: aws` | AWS CLI configured, EC2 instances |
| **GCP** | `compute_backend: gcp` | `gcloud` CLI configured |
| **Azure** | `compute_backend: azure` | `az` CLI configured |
| **Custom** | `compute_backend: custom` | Define in `hooks.py` |

## Hooks (`hooks.py`)

Each project can customize behavior via `hooks.py`:

```python
def run_research_iteration(orch) -> bool:
    """Custom research loop logic. Return True to continue, False to stop."""
    # Access orch.config, orch.state_dir, orch.run_agent(), etc.
    pass

def generate_figures_from_results(orch):
    """Custom figure generation from experiment data."""
    pass
```

If a hook function is not defined, ARK falls back to built-in behavior.
