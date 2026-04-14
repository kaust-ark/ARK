---
name: human-intervention
description: Protocol for requesting human intervention when an agent encounters a blocker it cannot resolve autonomously.
tags: [system, intervention, credentials, blocking]
---

# Human Intervention Protocol

## When to Request Human Intervention

Request human help when you encounter ANY of these situations:
- **Missing credentials**: API keys, tokens, or passwords needed to run experiments
- **Environment blockers**: Software that requires interactive setup (e.g., `openclaw onboard`)
- **Decision required**: A choice that affects the research direction and should not be made autonomously
- **Access denied**: Permissions, accounts, or registrations that only a human can obtain

## How to Request

Write a JSON file to `results/needs_human.json` with this format:

```json
{
  "type": "credential | environment | decision | access",
  "urgency": "blocking | degraded",
  "summary": "One-line description of what is needed",
  "details": "Full explanation of why this is needed and what experiments are affected",
  "needed_items": [
    {
      "key": "ANTHROPIC_API_KEY",
      "provider": "Anthropic",
      "purpose": "LLM inference for semantic analysis",
      "affected_experiments": ["exp2", "exp3"]
    }
  ],
  "fallback": "What to do if user does not respond (e.g., 'skip experiments that need this key', 'use Gemini as alternative LLM')",
  "timeout_minutes": 30
}
```

## After Writing the Request

- **If urgency is "blocking"**: STOP all work that depends on the missing resource. Continue with independent tasks if any exist.
- **If urgency is "degraded"**: Continue with reduced functionality, clearly noting what was skipped and why in your results.
- The pipeline will detect this file, notify the user via Telegram, and wait for a response.
- If the user responds, their input will be available in `results/human_response.json`.
- If no response within `timeout_minutes`, proceed with the `fallback` action.

## Important

- NEVER silently skip experiments due to missing resources. Always write `needs_human.json` first.
- NEVER fabricate data to fill gaps left by missing resources.
- Be specific about what is needed — vague requests waste time.
