"""PR5 acceptance — real SkyPilot provisioning across clouds + BYO-K8s.

This is the ONE place the ``skypilot`` pytest marker (added inert in PR1) is
finally exercised against real infrastructure. Everything else in the skypilot
suites (``test_skypilot_{seam,backend,launcher}.py``) runs against a mocked ``sky``
SDK and is CI-safe (unmarked); this file provisions real VMs / pods, costs money,
and is deselected in CI via ``-m "not skypilot"`` (see ``.github/workflows/ci.yml``).

It mirrors the gating philosophy of ``test_gcp_real.py``: **opt-in only**. Nothing
provisions unless the operator names the clouds to exercise, so a bare
``pytest -m skypilot`` on a laptop with creds still does nothing costly by accident.

Run it via the driver (recommended — adds preflight + an orphan sweep)::

    scripts/skypilot_acceptance.sh --clouds aws,gcp,kubernetes

or directly::

    ARK_SKYPILOT_ACCEPTANCE_CLOUDS=aws,gcp,kubernetes \
        pytest tests/integration/test_skypilot_acceptance.py -m skypilot -s

Optional per-cloud overrides (all default to "let SkyPilot pick the cheapest"):
    ARK_SKYPILOT_ACCEPTANCE_INSTANCE_<CLOUD>   e.g. ..._AWS=t3.small
    ARK_SKYPILOT_ACCEPTANCE_REGION_<CLOUD>     e.g. ..._GCP=us-central1
    ARK_SKYPILOT_ACCEPTANCE_SPOT=1             use spot for the provisioning legs
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

# Whole file is real-provisioning: deselected in CI, opt-in locally.
pytestmark = pytest.mark.skypilot

# SkyPilot must be importable at all (the `skypilot` extra) or there is nothing to
# test. This is a hard skip, distinct from the per-cloud opt-in below.
sky = pytest.importorskip("sky", reason="SkyPilot not installed ('pip install ark[skypilot]')")

from ark.compute.skypilot import SkyPilotBackend  # noqa: E402
from ark.compute._sky import load_sky, resolve_autostop  # noqa: E402

# Which clouds the operator has opted into exercising. Empty ⇒ skip everything
# (never provision by accident). Values map to the config `cloud:` string.
_CLOUDS = [c.strip().lower() for c in
           os.environ.get("ARK_SKYPILOT_ACCEPTANCE_CLOUDS", "").split(",") if c.strip()]

# Short idle backstop so that even if teardown is somehow skipped, SkyPilot
# self-downs quickly. Explicit teardown in the test is still the primary reaper.
_ACCEPTANCE_IDLE_MINUTES = int(os.environ.get("ARK_SKYPILOT_ACCEPTANCE_IDLE", "5"))


def _config_for(cloud: str) -> dict:
    """A minimal, cheap experiment backend config for one cloud.

    No accelerators and no pinned instance type by default → SkyPilot provisions
    the cheapest reachable CPU node, keeping the acceptance run inexpensive."""
    cc = {
        "type": "skypilot",
        "cloud": cloud,
        # Required autostop-down is set by the backend regardless; we only tune the
        # window down so a leaked cluster is cheap.
        "idle_minutes_to_autostop": _ACCEPTANCE_IDLE_MINUTES,
    }
    up = cloud.upper()
    inst = os.environ.get(f"ARK_SKYPILOT_ACCEPTANCE_INSTANCE_{up}")
    if inst:
        cc["instance_type"] = inst
    region = os.environ.get(f"ARK_SKYPILOT_ACCEPTANCE_REGION_{up}")
    if region:
        cc["region"] = region
    if os.environ.get("ARK_SKYPILOT_ACCEPTANCE_SPOT") == "1":
        cc["use_spot"] = True
    return {"experiment_compute_backend": cc}


def _cluster_records(cluster_name: str):
    """`sky status` records for one cluster (resolved through the async seam)."""
    from ark.compute._sky import resolve_request_value
    s = load_sky()
    return resolve_request_value(s, s.status(cluster_names=[cluster_name])) or []


def test_acceptance_clouds_opted_in():
    """Fail loudly if invoked with no clouds — a green run with 0 provisions would
    otherwise masquerade as 'acceptance passed'. Skips (not fails) so the marker
    can still be collected in a dry run."""
    if not _CLOUDS:
        pytest.skip(
            "No clouds opted in. Set ARK_SKYPILOT_ACCEPTANCE_CLOUDS=aws,gcp,kubernetes "
            "(or run scripts/skypilot_acceptance.sh --clouds ...).")


@pytest.mark.parametrize("cloud", _CLOUDS or ["<none-opted-in>"])
def test_provision_reachable_teardown_no_orphan(cloud, tmp_path):
    """Layer-1 SkyPilotBackend end-to-end on a real cloud / K8s cluster:
    provision → reachable over the SSH alias → explicit teardown → no orphan left.

    Covers the SKYPILOT_PLAN §5 acceptance bullets for one cloud: real provisioning,
    reachability, and teardown-with-no-orphaned-resources. Parametrized so the
    driver runs it across ≥2 clouds + a BYO-K8s context in one pass."""
    if cloud == "<none-opted-in>":
        pytest.skip("No clouds opted in (see ARK_SKYPILOT_ACCEPTANCE_CLOUDS).")

    project = f"acc-{cloud}-{int(time.time())}"
    code_dir = tmp_path / project
    (code_dir / "auto_research" / "state").mkdir(parents=True)

    config = _config_for(cloud)
    backend = SkyPilotBackend(config, project, code_dir)

    # The autostop-down window the backend WILL apply is required (no opt-out) for
    # experiment clusters — assert the policy before we spend money provisioning.
    autostop = resolve_autostop(config["experiment_compute_backend"], required=True)
    assert autostop["down"] is True, "experiment clusters must autostop-DOWN (sole reap path)"
    assert autostop["idle_minutes_to_autostop"] > 0

    provisioned = False
    try:
        print(f"\n[{cloud}] [1/4] provisioning cluster '{backend.cluster_name}'...")
        backend.setup()
        provisioned = True

        s = load_sky()
        assert backend._cluster_is_up(s), f"cluster '{backend.cluster_name}' did not come UP"
        state_file = code_dir / "auto_research" / "state" / "skypilot_cluster.yaml"
        assert state_file.exists(), "cluster-state file not written after setup()"
        print(f"[{cloud}] [2/4] cluster UP; state persisted.")

        # Reachability over SkyPilot's SSH alias — the same channel the experimenter
        # drives, so proving it here proves the agent-facing contract works.
        out = backend._ssh_exec("echo ARK_REACHABLE", timeout=60)
        assert "ARK_REACHABLE" in out, f"SSH alias unreachable; got: {out!r}"
        print(f"[{cloud}] [3/4] reachable over 'ssh {backend.cluster_name}'.")

    finally:
        if provisioned:
            print(f"[{cloud}] [4/4] tearing down '{backend.cluster_name}'...")
            backend.teardown()
            # No orphan: SkyPilot no longer knows this cluster, and state is cleared.
            leftover = _cluster_records(backend.cluster_name)
            assert not leftover, f"orphaned cluster after teardown: {leftover}"
            state_file = code_dir / "auto_research" / "state" / "skypilot_cluster.yaml"
            assert not state_file.exists(), "state file not cleared after teardown()"
            print(f"[{cloud}] teardown verified; no orphan.")
        else:
            print(f"[{cloud}] nothing provisioned; nothing to tear down.")
