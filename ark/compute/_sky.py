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

import contextlib
import logging
import re

_log = logging.getLogger("website.dashboard")

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


# ── async-request plumbing ───────────────────────────────────────────────────
# SkyPilot's client/server API (newer releases) returns an opaque *request id*
# from launch/down/status; the caller resolves it with ``get`` / ``stream_and_get``.
# Older releases run synchronously and return a value (tuple / list / None)
# directly. These two helpers absorb that difference for every ``type: skypilot``
# caller (Layer-1 backend + Layer-2 launcher), so neither has to re-derive the
# version handling. They differ deliberately in their error policy:
#
#   block_on_request     — for launch/teardown. Streams logs and lets errors
#                          PROPAGATE: a failed provision/teardown must abort the
#                          run rather than continue against a half-built cluster.
#   resolve_request_value — for status probes. Swallows errors to ``None`` so a
#                          flaky status check degrades to "unknown" instead of
#                          killing the run.


def block_on_request(sky, result):
    """Block on an async launch/teardown request id, streaming its logs.

    Errors are NOT swallowed — a failed launch or teardown must surface."""
    if isinstance(result, tuple) or result is None:
        return  # legacy synchronous API — already completed
    streamer = getattr(sky, "stream_and_get", None) or getattr(sky, "get", None)
    if streamer is not None:
        streamer(result)


def resolve_request_value(sky, result):
    """Resolve an async status-request id to its value, or pass a value through.

    Prefers ``get`` (no log streaming — this backs quick status probes) and
    deliberately swallows errors to ``None``: a flaky status check must degrade
    to "assume not up", never abort the run (the opposite policy from
    ``block_on_request``)."""
    if isinstance(result, (list, tuple)) or result is None:
        return result
    getter = getattr(sky, "get", None) or getattr(sky, "stream_and_get", None)
    if getter is not None:
        try:
            return getter(result)
        except Exception:
            return None
    return result


# ── workspace selection ──────────────────────────────────────────────────────
# SkyPilot "workspaces" (0.10+) isolate infra/credentials per team or project:
# each workspace pins a ``gcp.project_id`` (etc.), defined in the API server's
# ``~/.sky/config.yaml``. ARK maps one workspace per user (``ws-<user_id>``) so a
# launch lands in *that user's* GCP project via one central cross-project service
# account — no per-user key material.
#
# In SkyPilot's client/server model the active workspace only reaches the API
# server (which does the provisioning) if it rides along in the config the client
# uploads with each request: the server reads the ``active_workspace`` KEY out of
# the client's ``override_skypilot_config`` and resolves the workspace's
# ``gcp.project_id`` from there. The thread-local ``local_active_workspace_ctx``
# does NOT cross that boundary — ``skypilot_config.to_dict()`` (what the client
# actually sends) never reflects it, so the server falls back to the 'default'
# workspace and provisions into the host's own (central) project. We therefore set
# the ``active_workspace`` key via ``override_skypilot_config`` so it is uploaded.
#
# ``override_skypilot_config`` mutates the config held in the current SkyPilot
# *context* (contextvars), falling back to a process-GLOBAL when no context is
# active. To keep concurrent launches into different workspaces from racing on that
# global, we initialize a fresh (isolated) SkyPilot context first when none is
# active — our webapp launches run inside an ``asyncio.to_thread`` copied
# contextvars context, so the initialized context is per-launch and never leaks
# across threads (verified: overlapping launches stay isolated, the global stays
# 'default').
_WORKSPACE_UNSUPPORTED_WARNED = False


@contextlib.contextmanager
def active_workspace(sky, workspace):
    """Select ``workspace`` as the active SkyPilot workspace for the wrapped call.

    A context manager so a launch reads ``with active_workspace(sky, ws):
    sky.launch(...)``. No-ops (yields) when ``workspace`` is falsy — the caller
    then uses the ``default`` workspace / ambient credentials. If the installed
    SkyPilot predates workspaces (< 0.10, no ``override_skypilot_config`` /
    ``local_active_workspace_ctx``), it warns once and no-ops rather than failing
    the launch, so an older SDK still runs (against the host's own project) instead
    of hard-crashing."""
    global _WORKSPACE_UNSUPPORTED_WARNED
    if not workspace:
        yield
        return
    try:
        from sky import skypilot_config  # noqa: WPS433 (lazy, mirrors load_sky)
        from sky.utils import context as _sky_context  # noqa: WPS433
        override = getattr(skypilot_config, "override_skypilot_config", None)
        # Presence of the thread-local ctx marks a workspace-capable SDK (>= 0.10);
        # gate on it so an older SDK that has ``override_skypilot_config`` but no
        # workspaces can't hard-fail on an unknown ``active_workspace`` key.
        supports_workspaces = getattr(
            skypilot_config, "local_active_workspace_ctx", None) is not None
    except ImportError:
        override = None
        supports_workspaces = False
        _sky_context = None
    if override is None or not supports_workspaces:
        if not _WORKSPACE_UNSUPPORTED_WARNED:
            _log.warning(
                "SkyPilot workspace %r requested but the installed SkyPilot has no "
                "workspace support (needs >= 0.10); launching against the host's "
                "ambient credentials instead. Upgrade skypilot to isolate per-user "
                "projects.", workspace,
            )
            _WORKSPACE_UNSUPPORTED_WARNED = True
        yield
        return
    # Pick up a freshly-rendered workspace: a user who configures their GCP
    # project mid-session has ``ws-<id>`` written to ~/.sky/config.yaml
    # (skyworkspaces.render_sky_workspaces) but the long-lived webapp's in-process
    # config is cached from an earlier load, so ``override_skypilot_config`` would
    # reject the (as-yet-unseen) workspace. Reload first — file-locked and
    # concurrency-safe — so the loaded config matches what's on disk. Best-effort:
    # a reload hiccup must not sink the launch.
    reload = getattr(skypilot_config, "safe_reload_config", None)
    if reload is not None:
        try:
            reload()
        except Exception as e:  # noqa: BLE001 — never fail a launch on a reload blip
            _log.warning("SkyPilot config reload before workspace select failed "
                         "(continuing with cached config): %s", e)
    # Isolate the config override to this launch: without an active SkyPilot
    # context the override would mutate the process-global config and race with
    # concurrent launches into other workspaces (see module comment above).
    if _sky_context is not None and _sky_context.get() is None:
        _sky_context.initialize()
    with override({"active_workspace": workspace}):
        yield


# ── resource shaping ─────────────────────────────────────────────────────────
_CLOUD_ALIASES = {
    "aws": "AWS", "gcp": "GCP", "azure": "Azure",
    "kubernetes": "Kubernetes", "k8s": "Kubernetes",
}


def resolve_cloud(sky, cloud: str):
    """Map a config cloud string to a SkyPilot Cloud object (public API), or
    ``None`` for the empty string (let SkyPilot auto-select)."""
    if not cloud:
        return None
    attr = _CLOUD_ALIASES.get(cloud.lower(), cloud)
    cloud_cls = getattr(sky, attr, None)
    if cloud_cls is None:
        raise ValueError(f"Unknown SkyPilot cloud: {cloud!r}")
    return cloud_cls()


def build_resources(sky, cc: dict):
    """Build a ``sky.Resources`` from a compute-config dict. Every field is
    optional — SkyPilot infers/optimizes whatever is left unset. Shared by the
    Layer-1 experiment backend and the Layer-2 orchestrator launcher so the two
    read resource config identically."""
    kwargs: dict = {}
    cloud = resolve_cloud(sky, (cc.get("cloud") or "").strip())
    if cloud is not None:
        kwargs["cloud"] = cloud
    region = (cc.get("region") or "").strip()
    if region:
        kwargs["region"] = region
    accelerators = (cc.get("accelerators") or "").strip()
    if accelerators:
        kwargs["accelerators"] = accelerators
    instance_type = (cc.get("instance_type") or "").strip()
    if instance_type:
        kwargs["instance_type"] = instance_type
    if cc.get("use_spot"):
        kwargs["use_spot"] = True
    if cc.get("disk_size"):
        kwargs["disk_size"] = cc["disk_size"]
    image_id = (cc.get("image_id") or "").strip()
    if image_id:
        kwargs["image_id"] = image_id
    return sky.Resources(**kwargs)


# ── autostop / cost-safety ───────────────────────────────────────────────────
# Default idle window before SkyPilot auto-*downs* a cluster. This is the sole
# teardown path for a Layer-1 experiment cluster: it is launched from the
# orchestrator VM, so its SkyPilot state lives on that VM and the control plane
# has no record with which to reap it (SKYPILOT_PLAN §3). It is also a crash
# safety-net for the orchestrator cluster. Generous by default so an interactive
# experiment session between SSH commands is not reaped mid-run; tune via the
# ``idle_minutes_to_autostop`` config key.
DEFAULT_AUTOSTOP_IDLE_MINUTES = 60

# Config strings that explicitly turn autostop off (opt-out; ignored when the
# caller passes ``required=True``, i.e. experiment clusters).
_AUTOSTOP_OFF = {"off", "none", "disabled", "false", "no"}


def _coerce_idle_minutes(raw, default):
    """Coerce an ``idle_minutes_to_autostop`` config value to minutes.

    Returns an int > 0, or ``None`` to mean "disabled". Non-numeric / malformed
    values fall back to ``default`` rather than raising — this is a cost-safety
    net, so failing *closed* (keep the default autostop) beats aborting the
    launch. ``<= 0`` and the ``_AUTOSTOP_OFF`` strings disable it."""
    if raw is None:
        return default
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in _AUTOSTOP_OFF:
            return None
        try:
            raw = float(s)
        except ValueError:
            return default
    try:
        val = int(float(raw))
    except (TypeError, ValueError):
        return default
    return None if val <= 0 else val


def resolve_autostop(cc: dict, *, default_idle_minutes=DEFAULT_AUTOSTOP_IDLE_MINUTES,
                     required=False) -> dict:
    """Resolve the autostop policy for a launch into ``sky.launch`` kwargs.

    Returns a dict to splat into ``sky.launch`` — either
    ``{"idle_minutes_to_autostop": N, "down": True}`` (auto-teardown after N idle
    minutes) or ``{}`` when disabled. ``down=True`` rides with the idle window so
    the cluster is *terminated*, not merely stopped: a stopped cluster still bills
    for its disk, and a launcher-local experiment cluster stopped this way could
    never be downed from the control plane.

    Config keys (on the compute-backend block):
      ``idle_minutes_to_autostop`` — int minutes before auto-down; ``<= 0`` or one
                                     of ``off``/``none``/``disabled`` disables it.
      ``autostop_down``            — default True; False → STOP (keep disk) rather
                                     than DOWN. Ignored when ``required``.

    ``required=True`` (Layer-1 experiment clusters) forbids the opt-out: there is
    no cross-plane teardown fallback, so autostop-down is always applied — a
    disable/invalid value falls back to the default window instead of off."""
    idle = _coerce_idle_minutes(cc.get("idle_minutes_to_autostop"), default_idle_minutes)
    if idle is None:
        if not required:
            return {}
        idle = default_idle_minutes  # experiment clusters have no opt-out
    down = True if required else bool(cc.get("autostop_down", True))
    return {"idle_minutes_to_autostop": idle, "down": down}


# ── naming + setup shaping ───────────────────────────────────────────────────
def cluster_name(prefix: str, name: str) -> str:
    """A stable DNS-ish SkyPilot cluster name: ``<prefix><sanitized name>``.

    Sanitizes, **truncates, then strips** trailing/leading dashes — the strip
    comes last so a truncation boundary can't leave a trailing ``-`` (an invalid
    cluster name). Shared by the Layer-1 backend and Layer-2 launcher so both
    derive the *same* name for a given project (a re-run reconnects to, rather
    than duplicates, the cluster)."""
    safe = re.sub(r"[^a-z0-9-]", "-", name.lower())[:30].strip("-") or "project"
    return f"{prefix}{safe}"


def setup_script(commands) -> str:
    """Join a ``setup_commands`` list into a SkyPilot ``setup:`` block, dropping
    blank entries. Shared so both layers render the setup block identically."""
    return "\n".join(str(c) for c in (commands or []) if str(c).strip())
