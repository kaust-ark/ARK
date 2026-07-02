"""Project orchestrator state documents to the control plane (Phase 3, ADR-0013).

The orchestrator keeps writing its YAML under ``auto_research/state/`` as its own
authoritative working state (including crash recovery); after each iteration it
also pushes a *projection* of the documents the dashboard and export ZIP need, so
the control plane renders them without reading this process's disk. Best-effort —
a projection failure must never break a run.
"""

from __future__ import annotations

from pathlib import Path

import yaml

# state-dir filename → projection name (the ``{name}`` in /v1/.../state/{name})
_STATE_DOCS = {
    "paper_state.yaml": "paper_state",
    "action_plan.yaml": "action_plan",
    "findings.yaml": "findings",
    "memory.yaml": "memory",
    "dev_phase_state.yaml": "dev_phase_state",
}


def publish_state_docs(cp, state_dir, log=None) -> int:
    """Push each present state document to ``cp.put_state``. Returns the count."""
    state_dir = Path(state_dir)
    n = 0
    for fname, name in _STATE_DOCS.items():
        f = state_dir / fname
        if not f.exists():
            continue
        try:
            data = yaml.safe_load(f.read_text()) or {}
            if not isinstance(data, dict):
                data = {"value": data}
            cp.put_state(name, data)
            n += 1
        except Exception as e:  # noqa: BLE001 — projection is best-effort
            if log:
                log(f"state projection failed for {name}: {e}", "WARN")
    return n
