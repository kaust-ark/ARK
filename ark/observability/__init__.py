"""Observability — make each agent step visible.

OpenHands' ``--json`` mode already streams a JSONL event per action it takes
(bash command, file edit, observation, message). Today ARK captures that stream
only after the agent finishes and keeps just the final message. This package
parses the stream *as it arrives* into a clean, redacted step log so a human can
watch — live, in the CLI log, the webapp tail, or a Telegram digest — what the
agent is actually doing, instead of staring at a 30-minute blank.

- ``steps.parse_line``  — one JSONL line → a typed :class:`StepEvent` (or None).
- ``steps.Redactor``    — scrub secret values before anything is written.
- ``steps.StepLogger``  — write ``agent_steps.jsonl`` + emit human-readable
                          lines, gated by a verbosity level.
"""

from .steps import StepEvent, Redactor, StepLogger, parse_line

__all__ = ["StepEvent", "Redactor", "StepLogger", "parse_line"]
