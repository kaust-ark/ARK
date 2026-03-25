# The AI Scientist-v2

**Paper**: The AI Scientist-v2: Workshop-Level Automated Scientific Discovery via Agentic Tree Search
**Authors**: Yutaro Yamada*, Robert Tjarko Lange*, Cong Lu*, Shengran Hu, Chris Lu, Jakob Foerster, Jeff Clune, David Ha (Sakana AI, UBC, Vector Institute, Oxford)
**arXiv**: [2504.08066](https://arxiv.org/abs/2504.08066) (2025-04-14)
**Code**: [github.com/SakanaAI/AI-Scientist-v2](https://github.com/SakanaAI/AI-Scientist-v2)
**Relation to ARK**: Direct competitor — same goal, most well-known system in this space. Already cloned at `/home/xinj/AI-Scientist-v2/`

## Overview

End-to-end autonomous research system. First fully AI-generated paper to pass peer review (ICLR ICBINB workshop, score 6.33/10, top 45%). Successor to AI Scientist v1.

## Key Improvements Over v1

| Feature | v1 | v2 |
|---------|----|----|
| Codebase | Topic-specific templates | Domain-general, no templates |
| Execution | Linear | Tree-based (agentic tree search) |
| Parallelism | No | Yes (multiple nodes) |
| VLM feedback | No | Yes (figure refinement) |
| Evaluation | Not submitted | Workshop acceptance |

## Pipeline

### 1. Idea Generation (Section 3.1)
- More open-ended than v1 (abstract-level, not template-constrained)
- Integrates **Semantic Scholar** for literature review and novelty check
- Generates ~20 ideas per prompt, human selects top 3 to run

### 2. Experimentation — Agentic Tree Search (Section 3.2)
**Experiment Progress Manager** coordinates 4 stages:

- **Stage 1 — Preliminary Investigation**: Generate initial working prototype. 4 parallel nodes. Bug classification (buggy/non-buggy). Max iterations until basic prototype works
- **Stage 2 — Hyperparameter Tuning**: Optimize critical hyperparams from Stage 1's best node. Ends when training curves converge across 2+ datasets
- **Stage 3 — Research Agenda Execution**: Systematically implement core research agenda from Stage 2's best node. Runs until compute budget exhausted
- **Stage 4 — Ablation Studies**: Assess component importance. Track tested conditions to avoid repetition

**Tree Search Mechanics**:
- Each node = experiment plan + Python code + execution results + figures + VLM feedback
- Best-first search guided by LLM evaluation (performance metrics, training dynamics, plot quality)
- Parallel expansion: multiple nodes expanded concurrently
- Buggy nodes prioritized for debugging with probability threshold
- Non-buggy nodes refined/improved

**Specialized Node Types**:
- Hyperparameter nodes (Stage 2)
- Ablation nodes (Stage 4)
- Replication nodes (different random seeds for statistical robustness)
- Aggregation nodes (combine replication results, compute mean/std)

### 3. Writing (Section 3.3)
- Single-pass generation (replaced v1's incremental Aider-based approach)
- Reflection stage powered by reasoning models (o1)
- VLM feedback loop for figure quality and caption alignment
- Page-length adjustment via prompt with current PDF length + target

### 4. Review
- Enhanced AI reviewer with VLM integration
- Checks: figure-caption alignment, visual clarity, legend/label issues, figure duplication

## Models Used

| Role | Model |
|------|-------|
| Idea generation | Not specified (likely Claude/GPT) |
| Experiment coding | Claude 3.5 Sonnet |
| Node evaluation | LLM-based evaluator |
| Figure feedback | VLM (Vision-Language Model) |
| Writing reflection | o1 (reasoning model) |
| Review | AI reviewer + VLM |

## Costs
- ~$15-20 per experiment pipeline run (using Claude 3.5 Sonnet)
- Relatively cheap per run, but multiple seeds × multiple ideas adds up

## Results

### Workshop Submission (ICLR ICBINB 2025)
- 3 papers submitted among 43 total submissions
- 1 accepted: scores 6, 7, 6 (avg 6.33) — top 45%
- 2 rejected
- Accepted paper withdrawn post-review (ethical agreement with organizers)
- Topic: compositional regularization for neural networks (negative result)

### Limitations Found
- Citation hallucination (same as v1)
- Lacked methodological rigor in some experiments
- Could not formulate genuinely novel, high-impact hypotheses
- Workshop acceptance rate 60-80% (much easier than conference 20-30%)
- Human still needed to: select initial ideas, choose best manuscript per seed

## Comparison with ARK

| Aspect | AI Scientist-v2 | ARK |
|--------|-----------------|-----|
| Architecture | Monolithic pipeline + tree search | Mixin-based, 8 specialized agents |
| Experiment strategy | Agentic tree search (best-first) | Planner + Experimenter (linear + retry) |
| Parallelism | Multiple tree nodes concurrent | Researcher + Experimenter (2 threads) |
| Writing | Single-pass + o1 reflection | Writer agent + review loop |
| Figure handling | VLM feedback on generated plots | Dedicated visualizer agent + LaTeX geometry |
| Compute backend | Local Python execution | Flexible (local/SLURM) |
| Human involvement | Selects ideas + best manuscript | Provides config, more autonomous |
| Cost tracking | ~$15-20/run | cost_report.yaml with detailed breakdown |
| Self-repair | Debug nodes in tree | meta_debugger + self_repair agents |
| Paper compilation | LaTeX | LaTeX with CompilerMixin |

## Ideas Worth Adopting in ARK

1. **Agentic tree search for experiments** — Instead of linear retry, explore multiple experimental branches in parallel and select best. Most impactful architectural difference
2. **VLM figure feedback** — Use vision model to check figure quality, caption alignment, label clarity. Complement our visualizer agent
3. **Replication nodes** — Run best experiments with multiple seeds, aggregate mean/std. Statistical robustness
4. **Experiment Progress Manager** — Dedicated agent that manages experiment stages (prototype → tuning → agenda → ablation). More structured than our current planner
5. **Single-pass writing + reasoning model reflection** — May be more efficient than iterative writing
6. **Node-level metadata** — Each experiment node stores: plan, code, error trace, runtime, metrics, figures, VLM feedback, status. Rich provenance tracking
