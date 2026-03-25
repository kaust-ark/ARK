# Jr. AI Scientist

**Paper**: Jr. AI Scientist and Its Risk Report: Autonomous Scientific Exploration from a Baseline Paper
**Authors**: Atsuyuki Miyai, Mashiro Toyooka, Takashi Otonari, Zaiying Zhao, Kiyoharu Aizawa (University of Tokyo)
**arXiv**: [2511.04583](https://arxiv.org/abs/2511.04583) (v4, 2025)
**Relation to ARK**: Direct competitor — same goal (autonomous paper improvement from baseline), different architecture

## Overview

Jr. AI Scientist mimics a novice student researcher's workflow: given a baseline paper + code from a human mentor, it analyzes limitations, proposes improvements, runs experiments, and writes a new paper.

## Pipeline (4 phases)

### 1. Preparation
- Requires: baseline paper (with author permission), LaTeX source, codebase
- Minimal modifications to produce `baseline.py` and `plot.py`

### 2. Idea Generation
- LLM (o4-mini) analyzes baseline paper limitations
- **Novelty check via Semantic Scholar API** — reviews citing papers to avoid duplicating existing work
- Selects one idea to pursue

### 3. Experiment (3 stages)
- **Stage 1 — Idea Implementation**: 4 parallel nodes implement the idea → `proposed_method.py`. Max 12 iterations. Bug classification: Buggy/Non-Buggy, Plot-Buggy/Non-Plot-Buggy
- **Stage 2 — Iterative Improvement**: Coding agent proposes incremental enhancements → `improved_proposed_method.py`. Max 50 iterations. Stops when baseline is surpassed
- **Stage 3 — Ablation Study**: LLM generates hyperparameter + component ablation ideas, implemented via dedicated scripts

### 4. Writing
- BibTeX collection (Semantic Scholar + baseline bibliography)
- Method section written first (grounded in Stage 2 code)
- 3 rounds of reflection: logical consistency → formatting → figure quality (multimodal) → AI reviewer feedback
- Citation validation: compare with baseline, add missing refs, remove irrelevant ones
- Page-length adjustment: iterative gradual reduction (±1 page of target)

## Agents & Models

| Role | Model |
|------|-------|
| Coding agent | Claude Code v1.0.24 (Sonnet 4), 30 turns max |
| Idea generation | o4-mini |
| AI reviewer | GPT-4o |
| Writing | Claude Sonnet 4 |
| Paper evaluation | DeepReviewer (14B fine-tuned model) |
| Figure feedback | Large multimodal model |

## Key Results (DeepReviewer scores)

| System | Rating | Soundness | Presentation | Contribution |
|--------|--------|-----------|--------------|--------------|
| AI Scientist-v1 | 3.30 | 2.03 | 2.05 | 1.83 |
| AI Scientist-v2 | 2.75 | 1.67 | 1.50 | 1.58 |
| AI Researcher | 3.25 | 1.86 | 1.79 | 1.79 |
| CycleResearcher-12B | 3.92 | 2.25 | 2.25 | 2.04 |
| Zochi | 4.50 | 2.50 | 2.75 | 2.38 |
| **Jr. AI Scientist** | **5.75** | **2.75** | **2.75** | **2.75** |

Extended 3 baseline papers (LoCoOp NeurIPS'23, GL-MCM IJCV'25, Min-K%++ ICLR'25 spotlight).
Despite high DeepReviewer scores, papers were **rejected from Agents4Science** due to limited novelty, insufficient comparisons, and shallow theoretical justification.

## Risk Report (most valuable contribution)

### Experiment Risks
- Coding agents lack domain expertise → invalid "improvements" (e.g., batch-level normalization in zero-shot OOD detection)
- Only ~1 in 10 ideas actually works; high compute cost

### Writing Risks
- **Fabrication**: when reviewer requests new experiments, agent invents non-existent ablation studies — and AI reviewers cannot detect this
- **Citation hallucination**: newly-added BibTeX entries cited in irrelevant contexts
- **Result misinterpretation**: plausible but unfounded performance explanations
- Agent sometimes independently modifies BibTeX files, introducing non-existent papers

### Review Risks
- AI reviewers evaluate text only, cannot cross-check against actual code/data
- Fabricated experiments pass review undetected

## Comparison with ARK

| Aspect | Jr. AI Scientist | ARK |
|--------|-----------------|-----|
| Autonomy | Requires human mentor to select baseline | User provides config, more autonomous |
| Architecture | Monolithic with distinct phases | Mixin-based, 8 specialized agents |
| Novelty check | Semantic Scholar API | Not yet implemented |
| Parallel experiments | 4 competing nodes | Researcher + Experimenter (2 threads) |
| Visualization | Basic `plot.py` | Dedicated visualizer agent + LaTeX geometry |
| Cost tracking | Not detailed | cost_report.yaml |
| Writing reflection | 3-layer multi-pass | Single review loop |
| Citation validation | Dedicated post-writing pass | Not yet implemented |
| Page control | Iterative adjustment | Not yet implemented |
| Risk awareness | Comprehensive risk report | Meta-debugger + self-repair |

## Ideas Worth Adopting in ARK

1. **Novelty check** — Semantic Scholar API query before committing to an idea
2. **Multi-node parallel experiments** — fork 4 implementations, pick best
3. **Multi-layer writing reflection** — logic → format → figures → reviewer
4. **Citation validation pass** — post-writing cross-check
5. **Page-length iterative adjustment** — gradual trimming in CompilerMixin
6. **Result grounding** — writer can only reference files that actually exist (prevent fabrication)
