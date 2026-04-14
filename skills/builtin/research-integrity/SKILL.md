---
name: research-integrity
description: Rules for maintaining research integrity. No simulation, no fabrication, no shortcuts.
tags: [system, integrity, experiments, ethics]
---

# Research Integrity

## No Simulated Experiments

When the research requires evaluating a real system, you MUST use that system.
- Do NOT write `simulate_*()` functions to generate fake metrics
- Do NOT build a "prototype" or "simplified version" of the target system
- Do NOT use `np.random` to fabricate results

## Honest Failure Over Fabrication

If you cannot run an experiment:
- Report the specific error and what is needed
- Use the human-intervention skill to request help
- NEVER fill gaps with synthetic data

## Verify Your Results

After each experiment:
- Confirm the process actually ran (check logs, not just exit codes)
- Spot-check output data for sanity
- If results look suspiciously perfect, investigate

## Claim Traceability

Every quantitative claim in the paper must trace to a real result file:
- Copy exact values from `results/` files
- If a result does not exist, leave a `% TODO` comment — do not invent numbers

## Anonymous Submission

Papers must comply with double-blind review:
- No author names in the title
- Author block must be "Anonymous"
- No self-identifying references in body text
