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
  "type": "credential | installation | environment | decision | access",
  "urgency": "blocking",
  "summary": "One-line description of what is needed",
  "details": "Full explanation including exact error messages from failed attempts",
  "commands_tried": ["git clone ...", "npm install ..."],
  "error_output": "Paste the actual error output here",
  "needed_items": [
    {
      "key": "ANTHROPIC_API_KEY",
      "provider": "Anthropic",
      "purpose": "LLM inference for semantic analysis",
      "affected_experiments": ["exp2", "exp3"]
    }
  ],
  "timeout_minutes": 60
}
```

## After Writing the Request

- **STOP all work that depends on the missing resource.** Do not continue with alternative methods.
- Do NOT design your own workaround, fallback, or LLM-based substitute.
- The pipeline will notify the user via Telegram and wait for a response.
- If the user responds, their input will be available in `results/human_response.json`.
- Mark affected experiments as `"status": "blocked"` — not "completed_fallback" or "degraded".

## Critical Rules

- NEVER silently skip experiments due to missing resources. Always write `needs_human.json` first.
- NEVER fabricate data to fill gaps left by missing resources.
- NEVER create alternative evaluation methods (LLM-as-judge, text comparison, pattern matching) as substitutes for running the real system.
- Be specific about what is needed — include exact error messages from your installation attempts.
- The `"urgency"` field should almost always be `"blocking"`. Use of "degraded" is reserved for truly optional enhancements, not for core experiments.
