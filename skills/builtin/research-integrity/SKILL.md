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

## No LLM-as-Substitute

When the experiment plan requires running a real system (framework, platform, benchmark), you must NOT substitute it with an LLM call:
- Do NOT use "Gemini-as-judge" or "LLM-as-evaluator" as a replacement for running the actual system
- Do NOT create "fallback" code paths that replace real system interaction with text analysis
- Do NOT write scripts with `if not installed → use_llm_instead()` branches
- An experiment that calls an LLM instead of running the target system has NOT been conducted — it is fabrication with extra steps.

## Mandatory Installation Attempts

Before declaring any system unavailable:
- You MUST actually run the install commands (git clone, npm install, pip install, etc.)
- You MUST capture and report the exact error output
- `"install_attempted": false` is NEVER acceptable in environment_setup.json

## Honest Failure Over Fabrication

If you cannot run an experiment:
- Report the specific error and what is needed
- Use the human-intervention skill to request help with urgency "blocking"
- Mark the experiment as "blocked", NOT "completed_fallback" or "degraded"
- NEVER fill gaps with synthetic data or alternative evaluation methods

## Verify Your Results

After each experiment:
- Confirm the process actually ran (check logs, not just exit codes)
- Spot-check output data for sanity
- If results look suspiciously perfect, investigate
- If all results come from LLM API calls rather than running the target system, the experiment was not conducted

## Claim Traceability

Every quantitative claim in the paper must trace to a real result file:
- Copy exact values from `results/` files
- If a result does not exist, leave a `% TODO` comment — do not invent numbers

## Anonymous Submission

Papers must comply with double-blind review:
- No author names in the title
- Author block must be "Anonymous"
- No self-identifying references in body text
