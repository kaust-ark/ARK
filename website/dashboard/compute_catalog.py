"""Instance-type catalog: curated dropdown lists + validation for the cloud
backends the dashboard exposes (GCP, AWS).

The orchestrator VM (and, in the current phase, the experiments that run locally
on it) launches on whatever instance type the user picks. To keep that a safe,
predictable choice we offer a small curated dropdown per cloud AND let the user
type any instance type — validated against SkyPilot's catalog BEFORE we launch,
so a typo (or a type that doesn't exist / isn't in the region) fails fast in the
UI and at submit, rather than after a slow provision attempt.

SkyPilot is an OPTIONAL dependency (the ``skypilot`` extra). We import it lazily,
exactly like ``ark.compute._sky``, so importing this module never requires it. If
sky isn't installed we degrade to "unable to validate" (``valid=None``) rather
than hard-failing — the launch itself would then surface any real error.
"""

from __future__ import annotations

import logging
from typing import Optional, TypedDict

logger = logging.getLogger("website.dashboard")


class InstanceTypeOption(TypedDict):
    value: str      # the SkyPilot instance type, e.g. "n4-standard-2"
    label: str      # human label for the dropdown, e.g. "n4-standard-2 · 2 vCPU / 8 GB"
    vcpus: Optional[float]
    mem_gb: Optional[float]
    gpu: bool       # True for GPU-bearing types (shown with a badge)


# Per-cloud curated shortlists. Deliberately small — general-purpose sizes that
# comfortably run the orchestrator + local experiments, plus a couple of GPU
# options for compute-heavy runs. Users who need something else type it in
# (validated the same way). The FIRST entry of each list is the default when the
# user hasn't chosen — GCP's matches the historical pin (n4-standard-2).
_CURATED: dict[str, list[dict]] = {
    "gcp": [
        {"value": "n4-standard-2", "vcpus": 2, "mem_gb": 8, "gpu": False},
        {"value": "n4-standard-4", "vcpus": 4, "mem_gb": 16, "gpu": False},
        {"value": "n4-standard-8", "vcpus": 8, "mem_gb": 32, "gpu": False},
        {"value": "n2-standard-8", "vcpus": 8, "mem_gb": 32, "gpu": False},
        {"value": "c4-standard-8", "vcpus": 8, "mem_gb": 30, "gpu": False},
        {"value": "g2-standard-4", "vcpus": 4, "mem_gb": 16, "gpu": True},
    ],
    "aws": [
        {"value": "m6i.large", "vcpus": 2, "mem_gb": 8, "gpu": False},
        {"value": "m6i.xlarge", "vcpus": 4, "mem_gb": 16, "gpu": False},
        {"value": "m6i.2xlarge", "vcpus": 8, "mem_gb": 32, "gpu": False},
        {"value": "c6i.2xlarge", "vcpus": 8, "mem_gb": 16, "gpu": False},
        {"value": "g5.xlarge", "vcpus": 4, "mem_gb": 16, "gpu": True},
        {"value": "g4dn.xlarge", "vcpus": 4, "mem_gb": 16, "gpu": True},
    ],
}

SUPPORTED_CLOUDS = frozenset(_CURATED)


def _fmt_label(value: str, vcpus, mem_gb, gpu: bool) -> str:
    parts = [value]
    if vcpus and mem_gb:
        parts.append(f"{int(vcpus)} vCPU / {int(mem_gb)} GB")
    label = " · ".join(parts)
    return f"{label} · GPU" if gpu else label


def default_instance_type(cloud: str) -> Optional[str]:
    """The default instance type for a cloud (first curated entry), or None."""
    lst = _CURATED.get((cloud or "").lower())
    return lst[0]["value"] if lst else None


def curated_options(cloud: str) -> list[InstanceTypeOption]:
    """The curated dropdown shortlist for a cloud (empty for unsupported)."""
    out: list[InstanceTypeOption] = []
    for spec in _CURATED.get((cloud or "").lower(), []):
        out.append({
            "value": spec["value"],
            "label": _fmt_label(spec["value"], spec["vcpus"], spec["mem_gb"], spec["gpu"]),
            "vcpus": spec["vcpus"],
            "mem_gb": spec["mem_gb"],
            "gpu": spec["gpu"],
        })
    return out


class ValidationResult(TypedDict):
    valid: Optional[bool]   # True/False, or None when we couldn't check (sky absent)
    vcpus: Optional[float]
    mem_gb: Optional[float]
    error: str              # human message ("" when valid)


def validate(cloud: str, instance_type: str) -> ValidationResult:
    """Check that ``instance_type`` exists in ``cloud``'s SkyPilot catalog.

    Returns ``valid=True`` with vCPU/mem when it exists, ``valid=False`` with an
    error message when it doesn't (or the cloud is unsupported), and
    ``valid=None`` when we can't check (SkyPilot not installed, or a catalog
    lookup blew up) — callers treat None as "allow, can't verify" so a missing
    catalog never blocks a launch that would otherwise succeed.

    This is a BLOCKING call: the first catalog access downloads/refreshes CSVs
    (cached ~7h). Callers should run it off the event loop (run_in_threadpool).
    """
    cloud = (cloud or "").strip().lower()
    instance_type = (instance_type or "").strip()
    if not instance_type:
        return {"valid": False, "vcpus": None, "mem_gb": None,
                "error": "No instance type given."}
    if cloud not in SUPPORTED_CLOUDS:
        return {"valid": False, "vcpus": None, "mem_gb": None,
                "error": f"Unsupported cloud: {cloud or '(none)'}."}
    try:
        from sky import catalog  # lazy: skypilot is an optional extra
    except Exception:  # pragma: no cover — depends on install
        logger.warning("SkyPilot not importable; skipping instance-type validation "
                       "for %s/%s", cloud, instance_type)
        return {"valid": None, "vcpus": None, "mem_gb": None, "error": ""}
    try:
        exists = catalog.instance_type_exists(instance_type, clouds=cloud)
    except Exception as e:  # catalog fetch/network hiccup — don't block the launch
        logger.warning("Instance-type validation errored for %s/%s: %s",
                       cloud, instance_type, e)
        return {"valid": None, "vcpus": None, "mem_gb": None, "error": ""}
    if not exists:
        return {"valid": False, "vcpus": None, "mem_gb": None,
                "error": f"'{instance_type}' is not a valid {cloud.upper()} instance type."}
    vcpus = mem_gb = None
    try:
        vcpus, mem_gb = catalog.get_vcpus_mem_from_instance_type(instance_type, clouds=cloud)
    except Exception:
        pass
    return {"valid": True, "vcpus": vcpus, "mem_gb": mem_gb, "error": ""}
