---
name: human-intervention
description: Protocol for requesting human intervention when an agent encounters a blocker it cannot resolve autonomously.
tags: [system, intervention, credentials, blocking]
---

# Human Intervention Protocol

## When to Request Human Intervention

Request human help when you encounter ANY of these situations:
- **Missing credentials**: API keys, tokens, or passwords needed to run experiments
- **Installation failure**: A required system failed to install after real attempts (with actual error output)
- **Environment blockers**: Software that requires interactive setup, network access, or manual configuration
- **Decision required**: A choice that affects the research direction and should not be made autonomously
- **Access denied**: Permissions, accounts, or registrations that only a human can obtain

## How to Request

Write a JSON file to `results/needs_human.json` with this format:

```json
{
  "urgency": "blocker",
  "summary": "One-line description of what is blocked and why",
  "stage": "Phase label / pipeline step where this surfaced",
  "what_failed": "Exact command or condition that reproduces the blocker",
  "evidence": {
    "tested_commands": [
      {"cmd": "pip install foo", "exit_code": 1, "output": "…"}
    ],
    "error_output": "<truncated stderr if useful>"
  },
  "options": [
    {"id": "1",
     "title": "Provide API key ANTHROPIC_API_KEY",
     "consequence": "Unblocks exp2/exp3; user pastes the key"},
    {"id": "2",
     "title": "Defer these experiments to next iteration",
     "consequence": "Pipeline continues without them; paper claims weakened"}
  ],
  "default_option": "2",
  "timeout_minutes": 60
}
```

Each option is an action the **user** can take. The framework renders a
numbered Telegram menu; the user's reply is mapped back to the option
(or treated as free-text guidance if they write prose).

## After Writing the Request

- **STOP all work that depends on the missing resource.** Do not continue with alternative methods.
- Do NOT design your own workaround, fallback, or LLM-based substitute.
- The pipeline notifies the user via Telegram and waits for a response.
- The user's decision lands in `auto_research/state/hitl_decisions.yaml`.
  Read this file before retrying any previously-blocked experiment;
  if the decision was "deferred", mark it deferred and do not retry.
- Mark affected experiments as `"status": "blocked"` — not "completed_fallback" or "degraded".

## Critical Rules

- NEVER silently skip experiments due to missing resources. Always write `needs_human.json` first.
- NEVER fabricate data to fill gaps left by missing resources.
- NEVER create alternative evaluation methods (LLM-as-judge, text comparison, pattern matching) as substitutes for running the real system.
- Be specific about what is needed — include exact error messages from your installation attempts.
- The `"urgency"` field should almost always be `"blocking"`. Use of "degraded" is reserved for truly optional enhancements, not for core experiments.
