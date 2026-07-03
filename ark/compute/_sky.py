"""Lazy SkyPilot SDK seam (folded Phases 5+6, ADR-0010).

SkyPilot (the ``sky`` package) is an optional dependency behind the ``skypilot``
extra. Importing ``ark.compute`` — including building a backend from config on the
*control plane* to answer a local project's request — must never require SkyPilot
to be installed. So, exactly like the object-store provider SDKs (ADR-0012), the
SDK is imported only on first real use (cluster launch / status / teardown), not
at module import.

PR1 is scaffolding: this module exists so the ``type: skypilot`` backends landing
in PR2/PR3 have a single, tested import point. Callers use::

    from ark.compute._sky import load_sky
    sky = load_sky()          # raises a helpful error if the extra is missing
    sky.launch(task, ...)
"""

from __future__ import annotations

_EXTRA_HINT = (
    "SkyPilot is not installed, but a compute backend is configured with "
    "type: skypilot. Install the optional dependency — e.g. "
    "`pip install 'ark[skypilot]'` (then add the cloud extras you use, such as "
    "`pip install 'skypilot[gcp,aws,kubernetes]'`)."
)


def load_sky():
    """Import and return the SkyPilot SDK, or raise a clear install hint.

    Kept as a function (not a module-level ``import sky``) so ``ark.compute``
    imports with no cloud SDKs present; the ``skypilot`` extra is only needed once
    a ``type: skypilot`` backend is actually launched."""
    try:
        import sky  # noqa: WPS433 (intentional lazy import)
    except ImportError as exc:  # pragma: no cover — exercised via monkeypatched import
        raise RuntimeError(_EXTRA_HINT) from exc
    return sky
