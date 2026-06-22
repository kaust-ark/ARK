"""Intervention subsystem — pre-action guardrails for autonomous runs.

ARK runs agents (via OpenHands) and an orchestrator loop that, left fully
autonomous, can take consequential actions: delete files, launch many compute
jobs, provision paid cloud instances, push code to a remote, or hand over
credentials. This package adds a *policy + approval gate* so the high-stakes
ones pause and ask a human first.

Layers (see ARCHITECTURE.md → Intervention):

- ``policy``   — pure risk classification: command/action → (category, severity)
                 → decision (allow / notify / ask / deny) under an autonomy level.
- ``gate``     — given an ``ask`` decision, relay an ApprovalRequest to a human
                 over a pluggable channel (Telegram today; webapp later) and wait
                 for the verdict, with a timeout + safe default. Records the
                 decision so identical asks are not repeated (approval memory).
- ``wrappers`` — shadow-PATH shims (``rm``/``sbatch``/``gcloud``/…) that route an
                 agent's risky command through the policy + gate *before* the real
                 binary runs.

The gate fails *open* when no approval channel is configured, so existing
projects and the test suite are never blocked by an unanswerable prompt.
"""

from .policy import (
    Category,
    Severity,
    Decision,
    InterventionPolicy,
    load_policy,
    redact_secrets,
)

__all__ = [
    "Category",
    "Severity",
    "Decision",
    "InterventionPolicy",
    "load_policy",
    "redact_secrets",
]
