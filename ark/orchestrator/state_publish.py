"""Project orchestrator state documents to the control plane (Phase 3, ADR-0013).

The orchestrator keeps writing its YAML under ``auto_research/state/`` as its own
authoritative working state (including crash recovery); after each iteration it
also pushes a *projection* of the documents the dashboard and export ZIP need, so
the control plane renders them without reading this process's disk. Best-effort —
a projection failure must never break a run.

The projection is also the rehydration source: when a run's VM dies and a fresh
one is provisioned (the disk starts empty), :func:`rehydrate_state_docs` pulls
these documents back so the orchestrator can resume from where it left off
instead of restarting at iteration 0. ``checkpoint`` and ``research_state`` are
included precisely because they carry the resume pointer.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ark.orchestrator.state import _atomic_write_yaml

# state-dir filename → projection name (the ``{name}`` in /v1/.../state/{name}).
# Order matters for rehydration only in that it is stable; each doc is independent.
_STATE_DOCS = {
    "checkpoint.yaml": "checkpoint",
    "research_state.yaml": "research_state",
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
                # These state files are always mappings; skip anything else
                # rather than wrap it and change the doc's shape in the export ZIP.
                if log:
                    log(f"state projection skipped {name}: not a mapping", "WARN")
                continue
            cp.put_state(name, data)
            n += 1
        except Exception as e:  # noqa: BLE001 — projection is best-effort
            if log:
                log(f"state projection failed for {name}: {e}", "WARN")
    return n


def rehydrate_state_docs(cp, state_dir, log=None) -> int:
    """Fill any *missing* local state document from the control-plane projection.

    Run once at orchestrator startup so a freshly provisioned VM (the run's prior
    VM died or was preempted, leaving this disk empty) reconstructs
    ``auto_research/state/`` before resume — otherwise the run silently restarts
    from iteration 0. Only writes a doc that is absent locally: a present local
    file is the authoritative working copy and is never overwritten here (the
    resume pointer specifically is reconciled separately, newest-wins, so a stale
    local checkpoint can't pin the run to an old iteration). Returns the count
    rehydrated. Best-effort — a failure must never break a run.
    """
    state_dir = Path(state_dir)
    n = 0
    for fname, name in _STATE_DOCS.items():
        f = state_dir / fname
        if f.exists():
            continue
        try:
            data = cp.get_state(name)
        except Exception as e:  # noqa: BLE001 — rehydration is best-effort
            if log:
                log(f"state rehydrate failed for {name}: {e}", "WARN")
            continue
        if not isinstance(data, dict) or not data:
            continue
        try:
            _atomic_write_yaml(f, data, default_flow_style=False, allow_unicode=True)
            n += 1
            if log:
                log(f"rehydrated {fname} from control plane", "INFO")
        except Exception as e:  # noqa: BLE001
            if log:
                log(f"state rehydrate write failed for {name}: {e}", "WARN")
    return n
