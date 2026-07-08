"""SkyPilot orchestrator launcher (folded Phases 5+6, ADR-0010, PR3).

A *fresh implementation* of the `JobLauncher` seam on SkyPilot's task model —
**not** an adapter over ``CloudVmJobLauncher`` (SSH + rsync + gcloud). The
orchestrator process runs on a SkyPilot-provisioned cluster:

- ``launch()``  → build a ``sky.Task`` whose ``run:`` command is
                  ``python -m ark.orchestrator`` (code via ``workdir``, the
                  project dir + control-plane token via ``file_mounts``, API keys
                  via task ``envs``, cloud/accelerators/spot via ``Resources``),
                  and ``sky.launch`` it onto a named cluster; the async request is
                  blocked on through provisioning + setup + job submission, so
                  provisioning blocks but the long-lived run is left going in the
                  cluster's job queue; handle ``skypilot:{cluster}``.
- ``poll()``    → ``sky status`` on the cluster, normalized onto the module
                  constants. Like the cloud path, this is a liveness/crash probe:
                  the remote orchestrator self-reports its terminal outcome into
                  the control-plane DB over /v1 (ADR-0013), which is authoritative.
- ``cancel()``  → ``sky down`` the cluster, then ``on_complete`` — same threaded
                  ordering as the cloud launcher so a delete-endpoint ``rmtree``
                  can't run before teardown has read the project dir.

Mirrors ``CloudVmJobLauncher``: no local log to watch (``log_glob`` unset ⇒ the
remote run reports home over /v1), ``initial_status`` = RUNNING (``sky.launch``
with ``retry_until_up`` blocks until the cluster is UP and the run has started).

The GCP ``cloud`` path stays default and untouched; ``type: skypilot`` is
additive and default-off. The ``sky`` SDK is imported lazily (``_sky.load_sky``)
so importing this module never requires the ``skypilot`` extra.
"""

from __future__ import annotations

import logging
import os
import shlex
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

from .base import JobLauncher, LaunchSpec, PollResult, RUNNING, GONE, UNKNOWN
# Pure (non-SDK) shaping helpers, shared with the Layer-1 backend so both derive
# the same cluster name / setup block. The SDK-touching helpers (load_sky,
# build_resources, …) stay lazily imported inside methods to keep the extra optional.
from ark.compute._sky import cluster_name as _cluster_name_for, setup_script as _setup_script_for

# Same logger the cloud launcher uses, so existing ERROR-keyed alerting on
# unreachable-VM / stuck-run incidents keeps firing across both cloud paths.
_log = logging.getLogger("website.dashboard")

# Where SkyPilot lands the Task's ``workdir`` (the ARK source tree) on the head
# node — relative to $HOME so it resolves for any cloud's default SSH user.
_REMOTE_WORKDIR = "~/sky_workdir"
# The file-mount destination for the project dir on the cluster. A fixed name
# (not the project id) so the run command never interpolates a user-derived value
# into the remote path — no quoting/injection surface.
_REMOTE_PROJECT_DIRNAME = "ark_project"
# The control-plane bearer token rides as a file-mounted secret (never on argv,
# where it would show up in ``ps`` — same rule as the cloud path's RAM-disk .env),
# and the run command sources it into ``ARK_CONTROL_PLANE_TOKEN``.
_REMOTE_CP_TOKEN = "~/.ark_cp_token"


class SkyPilotVmJobLauncher(JobLauncher):
    """Run the orchestrator on a SkyPilot cluster (any cloud or K8s).
    Handle: ``skypilot:{cluster}``.

    The remote run shares no filesystem/DB with the control plane, so it reports
    status/state/artifacts home over the /v1 API (Phase 1/3); poll here is a pure
    ``sky status`` liveness probe and the DB is authoritative for terminal outcome.

    ``initial_status`` = RUNNING (base default); ``log_glob`` unset (no local log)."""

    def __init__(self, log_fn: Optional[Callable] = None):
        self.log = log_fn or _log.info

    @staticmethod
    def _cluster_of(handle: str) -> str:
        """Cluster name out of a ``skypilot:{cluster}`` handle."""
        return handle.split(":", 1)[1] if ":" in handle else handle

    def _cluster_name(self, spec: LaunchSpec) -> str:
        """Config-provided cluster name, else a stable DNS-ish name per project so
        a re-run reconnects to (not duplicates) the cluster. Uses the same shared
        sanitizer as the Layer-1 backend (distinct ``ark-orch-`` prefix)."""
        cc = (spec.config or {}).get("orchestrator_compute_backend", {}) or {}
        if cc.get("cluster_name"):
            return str(cc["cluster_name"])
        return _cluster_name_for("ark-orch-", spec.project_id)

    # ── launch ───────────────────────────────────────────────────────────────
    def launch(self, spec: LaunchSpec) -> str:
        from ark.compute import validate_config
        from ark.compute._sky import (
            load_sky, block_on_request, build_resources, resolve_autostop,
            active_workspace)
        from website.dashboard.jobs import control_plane_transport, api_keys_to_env

        config = spec.config
        if config is None:
            raise RuntimeError("SkyPilotVmJobLauncher.launch requires spec.config (config.yaml)")
        validate_config(config)

        cc = config.get("orchestrator_compute_backend", {}) or {}
        cluster = self._cluster_name(spec)
        pdir = Path(spec.project_dir)
        # The user's SkyPilot workspace (``ws-<user_id>``) pins which GCP project
        # this launch provisions into; the central launcher SA has cross-project
        # access, so selecting the workspace is all that routes the run to the
        # user's project. Empty ⇒ the 'default' workspace / host credentials.
        workspace = (cc.get("workspace") or "").strip()

        # Control-plane transport: the cluster shares no FS/DB, so it must report
        # over the /v1 API. Without a configured URL the run is blind — warn loudly
        # (same contract as the cloud launcher).
        cp_url, cp_token = control_plane_transport(spec.project_id, spec.settings)
        if not cp_url:
            _log.warning(
                f"SkyPilot orchestrator {spec.project_id} launched without a control-plane "
                f"URL (settings.control_plane_url unset): the remote run cannot report "
                f"status/state/artifacts back and the dashboard will not see progress."
            )

        envs = api_keys_to_env(spec.api_keys or {})
        self._warn_missing_oauth_credentials(spec, envs)

        sky = load_sky()
        ark_root = str(Path(__file__).resolve().parents[2])  # repo root containing ark/

        # The token is minted per-run; upload it as a file-mounted secret. Written
        # to a temp file only for the duration of the launch, then wiped (the
        # finally covers a setup/launch failure so the plaintext token never lingers).
        token_file: Optional[str] = None
        try:
            # Fixed remote dirname (not the project id) → no user-derived value in
            # the remote path, so nothing to quote/escape at the mount or run site.
            file_mounts = {f"~/{_REMOTE_PROJECT_DIRNAME}": str(pdir)}
            if cp_token:
                fd, token_file = tempfile.mkstemp(prefix="ark_cp_token_")
                with os.fdopen(fd, "w") as fh:
                    fh.write(cp_token)
                os.chmod(token_file, 0o600)
                file_mounts[_REMOTE_CP_TOKEN] = token_file

            task = sky.Task(
                name=cluster,
                setup=_setup_script_for(cc.get("setup_commands")) or None,
                run=self._run_command(spec, cc, cluster, cp_url),
                workdir=ark_root,
                envs=envs or None,
            )
            task.set_resources(build_resources(sky, cc))
            task.set_file_mounts(file_mounts)

            self.log(
                f"Launching SkyPilot orchestrator cluster '{cluster}' "
                f"(cloud={cc.get('cloud') or 'auto'}, "
                f"workspace={workspace or 'default'}, project={spec.project_id})..."
            )
            # sky.launch (0.7+ client/server API) submits the task to the cluster's
            # job queue and returns an async request id; block_on_request blocks on
            # provisioning + setup + job submission and returns while the long-lived
            # orchestrator run keeps going in the queue — i.e. the "detach" behaviour
            # the removed detach_run kwarg used to provide (mirrors the Layer-1
            # SkyPilotBackend._launch). retry_until_up rides out transient capacity
            # errors like the GCP path. Autostop-down is a crash safety-net: the
            # orchestrator runs as a queued job, so SkyPilot's idle timer only starts
            # once that job exits — a normal run is never reaped mid-flight, but a
            # crashed one that outlives cancel()'s reach still self-downs. Opt-out
            # allowed here (the control plane CAN `sky down` this cluster via
            # cancel()), unlike the experiment backend where autostop is the only reap.
            autostop = resolve_autostop(cc)
            # Select the user's workspace for this launch only (thread-local, so
            # concurrent launches into different projects don't race).
            with active_workspace(sky, workspace):
                result = sky.launch(
                    task, cluster_name=cluster, retry_until_up=True, **autostop,
                )
                block_on_request(sky, result)
        finally:
            if token_file:
                try:
                    os.unlink(token_file)
                except OSError:
                    pass

        return f"skypilot:{cluster}"

    @staticmethod
    def _warn_missing_oauth_credentials(spec: LaunchSpec, envs: dict) -> None:
        """Surface the PR3 credential gap instead of failing auth silently.

        ``api_keys_to_env`` forwards API keys and the Claude OAuth token as env
        vars, but *file-based* OAuth sessions — Gemini's ``gemini_oauth_json`` (→
        ``~/.gemini/oauth_creds.json``) — are not yet provisioned onto the cluster
        (that is PR4 secret injection, which the cloud launcher does via
        ``provision_gemini_session``). Warn loudly when a project relies on Gemini
        OAuth with no Gemini/Google API key to fall back on, so an operator sees why
        auth will fail rather than debugging a silent failure on the remote run."""
        keys = spec.api_keys or {}
        gemini_oauth_only = bool(keys.get("gemini_oauth_json")) and not (
            envs.get("GEMINI_API_KEY") or envs.get("GOOGLE_API_KEY")
        )
        if gemini_oauth_only:
            _log.warning(
                f"SkyPilot orchestrator {spec.project_id}: project uses Gemini OAuth "
                f"(gemini_oauth_json) but no Gemini/Google API key — the OAuth session "
                f"is not provisioned onto the SkyPilot cluster yet (PR4), so Gemini agent "
                f"calls will fail auth. Set a GEMINI/GOOGLE API key or use type: cloud."
            )

    def _run_command(self, spec: LaunchSpec, cc: dict, cluster: str, cp_url: str) -> str:
        """The orchestrator start command that runs on the cluster.

        Runs ``python -m ark.orchestrator`` from the synced ARK source
        (``workdir`` → ``~/sky_workdir``) against the file-mounted project dir. When
        a control-plane URL is configured, ``--control-plane-url`` + ``--project-id``
        wire the /v1 reporting path; the bearer token is sourced from the mounted
        secret file rather than passed on argv."""
        # Fixed remote path (see _REMOTE_PROJECT_DIRNAME) — no interpolation of the
        # project id into the shell command, so no quoting/injection surface here.
        remote_project = f"$HOME/{_REMOTE_PROJECT_DIRNAME}"
        max_iterations = int(spec.max_iterations or cc.get("max_iterations", 3) or 3)
        # Coerce to float so a non-numeric config value fails loudly at launch
        # rather than injecting shell text into the run command.
        max_days = float((spec.config or {}).get("max_days") or 3)

        cp_args = ""
        if cp_url:
            cp_args = (
                f"--control-plane-url {shlex.quote(cp_url)} "
                f"--project-id {shlex.quote(spec.project_id)} "
            )
        return (
            f"cd {_REMOTE_WORKDIR} && "
            f"export PYTHONPATH={_REMOTE_WORKDIR} && "
            # The agent runtime (`openhands`) is installed as a uv tool into
            # ~/.local/bin by the setup block; the run shell is separate from setup
            # so put it on PATH here, else the orchestrator can't find the binary
            # and exits on first agent call (ark/pipeline.py fails fast).
            f"export PATH=\"$HOME/.local/bin:$PATH\" && "
            # Source the mounted token (absent ⇒ blind run; see the launch warning).
            f"export ARK_CONTROL_PLANE_TOKEN=\"$(cat {_REMOTE_CP_TOKEN} 2>/dev/null || true)\" && "
            f"python -m ark.orchestrator "
            f"--project {shlex.quote(spec.project_id)} "
            f"{cp_args}"
            f"--project-dir {remote_project} "
            f"--code-dir {remote_project} "
            f"--iterations {max_iterations} "
            f"--max-days {max_days}"
        )

    # ── poll ─────────────────────────────────────────────────────────────────
    def poll(self, handle: str, project_dir: Path) -> PollResult:
        from ark.compute._sky import load_sky, resolve_request_value

        cluster = self._cluster_of(handle)
        try:
            sky = load_sky()
            records = resolve_request_value(sky, sky.status(cluster_names=[cluster]))
        except Exception as e:  # transient SDK/network error — retry next cycle
            _log.error(f"SkyPilot orchestrator poll failed for {cluster}: {e}")
            return PollResult(UNKNOWN, "poll-error")

        # ``resolve_request_value`` swallows a flaky async status-request error to
        # ``None`` (its documented "assume not up" policy) — distinct from a
        # genuine empty list. Treat None as transient (UNKNOWN, retry) so a status
        # blip can NOT trip the GONE crash-safety-net and kill a live run. Only a
        # real empty result — the cluster truly unknown to SkyPilot — is GONE.
        if records is None:
            return PollResult(UNKNOWN, "status-unavailable")
        if not records:
            # Cluster no longer known to SkyPilot — treat as the process vanishing.
            # The control-plane DB is authoritative for the outcome (crash-safety-net).
            return PollResult(GONE, "no-cluster")

        status = records[0].get("status")
        name = getattr(status, "name", str(status)).upper()
        # UP → alive. STOPPED/DOWN → gone (DB authoritative). INIT/other →
        # indeterminate (still provisioning): leave the project as-is and retry.
        if name.endswith("UP"):
            state = RUNNING
        elif "STOP" in name or "DOWN" in name:
            state = GONE
        else:
            state = UNKNOWN
        return PollResult(state, name)

    # ── cancel ───────────────────────────────────────────────────────────────
    def cancel(self, handle: str, project_dir: Path, on_complete=None) -> None:
        """Tear the orchestrator cluster down in a background thread (``sky down``
        kills the VM the orchestrator PID lives on), then run ``on_complete``.

        Teardown reads nothing from ``project_dir``, but ``on_complete`` (e.g. the
        delete endpoint's ``rmtree``) still runs *after* the async teardown so the
        ordering matches the cloud launcher and a future teardown that does read
        the dir can't be raced.

        NOTE: this only reaps the *orchestrator* cluster. A Layer-1 SkyPilot
        *experiment* cluster the run provisioned is launched from the orchestrator
        VM, so its SkyPilot state lives on that VM — the control plane has no record
        of it and cannot ``sky down`` it here. Reaping it relies on the required
        autostop-down window the Layer-1 backend sets at launch (``resolve_autostop
        (..., required=True)``); the cloud launcher's cross-plane
        ``_teardown_experiment_vm`` has no equivalent for SkyPilot's launcher-local
        state model."""
        cluster = self._cluster_of(handle)

        def _run():
            try:
                from ark.compute._sky import load_sky, block_on_request
                sky = load_sky()
                self.log(f"Tearing down SkyPilot orchestrator cluster '{cluster}'...")
                block_on_request(sky, sky.down(cluster))
                self.log(f"Cluster '{cluster}' torn down")
            except Exception as e:
                _log.error(f"Failed to teardown SkyPilot cluster '{cluster}': {e}", exc_info=True)
            if on_complete:
                try:
                    on_complete()
                except Exception as e:
                    _log.error(f"cancel on_complete failed for {cluster}: {e}", exc_info=True)

        threading.Thread(target=_run, daemon=True).start()
