"""Apptainer experiment sandbox — two lanes, one advisory and one structural.

Runs agent-generated experiment code inside a prebuilt container image instead
of on the bare host — for host hygiene, reproducibility, and isolation of
untrusted/agent-generated code.

ADVISORY lane (the original, still the default)
    A per-project ``sandbox/run.sh`` helper plus a directive in the
    experimenter/coder prompts asking the agent to prefix its commands with it.

    This does not hold. Counted on project 76759cf7: 14 commands executed, 0
    of them through ``sandbox/run.sh``. The directive is a request, and the
    agent that ignores it is simply outside the sandbox — with no error, no
    log line, and nothing to notice afterwards. Isolation you have to ask for
    is not isolation.

STRUCTURAL lane (opt-in via ``ARK_AGENT_SANDBOX=apptainer``)
    OpenHands' own ``ApptainerWorkspace`` runs the agent-server INSIDE the
    container and drives its tools over the container's HTTP API. Every bash
    and file tool call therefore executes in the container by construction:
    there is no command the agent can type that lands on the host, because the
    process that would run it does not live there. The prompt stops being the
    enforcement mechanism.

    It needs the SDK runtime (``ARK_AGENT_RUNTIME=sdk``), because the workspace
    is bound to a Conversation and only ``sdk_driver.py`` builds one, plus a
    one-off per-node image pull (the exact command comes back in
    ``structural_sandbox_status()``'s reason when the image is missing).

Both lanes degrade to bare-host execution rather than failing a run when
Apptainer or the image is missing — the structural lane says so loudly in the
run log. Fail-CLOSED (refuse to run untrusted code without a sandbox) waits
until the image is guaranteed present on every node.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional, Tuple

# Built once via `apptainer build` (see .ark/data/_sandbox/ark-sci.def). Override
# per-node with ARK_SANDBOX_SIF. Contains python3.12 + numpy/scipy/scikit-learn/
# pandas/matplotlib/statsmodels.
DEFAULT_SIF = "/data/fat/ark/ARK/.ark/data/_sandbox/ark-sci.sif"
APPTAINER_FALLBACK = "/data/secure/bin/apptainer"

# The structural lane needs an image that SERVES the OpenHands agent-server:
# ApptainerWorkspace launches it with `apptainer run <sif> --host ... --port ...`,
# so the image's runscript has to be the server itself. Our own ark-sci.def is a
# bare scientific-python image with no runscript and cannot be used here — and
# does not need to be, since the project's conda env is bind-mounted in below
# and brings the whole scientific stack with it.
#
# PINNED to the commit that built our installed SDK (agent-sdk v1.21.0 ==
# 4110929), and it has to stay pinned to whatever `openhands` we run. The
# obvious `:latest-python` was tried first and the run died on
#
#     ValidationError: 1 validation error for Event
#     parent_id  Extra inputs are not permitted
#
# — a newer server sending an event field that our client's Event model, which
# is `extra="forbid"`, refuses. The agent kept working inside the container but
# almost nothing reached the callback, so the phase came back empty. Client and
# server here are two halves of one protocol; a floating tag silently upgrades
# one of them.
AGENT_SERVER_IMAGE = "ghcr.io/openhands/agent-server:4110929-python"


def sandbox_sif_path() -> str:
    return os.environ.get("ARK_SANDBOX_SIF", DEFAULT_SIF)


def apptainer_bin() -> Optional[str]:
    found = shutil.which("apptainer")
    if found:
        return found
    if Path(APPTAINER_FALLBACK).exists():
        return APPTAINER_FALLBACK
    return None


def sandbox_available() -> bool:
    """True when both Apptainer and the base image are present on this node."""
    return bool(apptainer_bin()) and Path(sandbox_sif_path()).is_file()


# --- structural lane ---------------------------------------------------------


def agent_server_sif() -> Path:
    """Where the agent-server SIF lives on this node.

    Same naming rule ApptainerWorkspace uses for its own cache, so a SIF pulled
    by hand and one pulled by the SDK are the same file rather than two 4 GB
    copies.
    """
    override = os.environ.get("ARK_AGENT_SERVER_SIF")
    if override:
        return Path(override)
    cache = Path(os.environ.get("ARK_AGENT_SERVER_CACHE")
                 or (_account_home() / ".apptainer_cache"))
    return cache / (AGENT_SERVER_IMAGE.replace(":", "_").replace("/", "_") + ".sif")


def _account_home() -> Path:
    """The real account home, ignoring any ``HOME`` override.

    ``Path.home()`` reads ``$HOME``, and the launcher deliberately rewrites
    ``HOME`` to the PROJECT directory so an agent's stray dotfiles stay with
    the project. The SIF is a machine-level asset shared by every project, so
    resolving it through ``$HOME`` sent the lookup into the project directory
    and reported the image missing on a node where it was already pulled —
    the structural sandbox then silently degraded to the advisory one on
    every real run, while standalone tests (no HOME override) passed.
    """
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path.home()


def structural_sandbox_requested() -> bool:
    return os.environ.get("ARK_AGENT_SANDBOX", "").strip().lower() == "apptainer"


def structural_sandbox_status() -> Tuple[bool, str]:
    """``(active, reason)`` — the reason is a line fit for the run log.

    Never raises and never pulls: a 4 GB image download in the middle of a
    phase would look exactly like a hung agent. The image is a one-off node
    setup step, and the reason string spells out the command.
    """
    if not structural_sandbox_requested():
        return False, "not requested (ARK_AGENT_SANDBOX unset)"
    # The workspace is an argument to the SDK driver's Conversation; the stock
    # headless CLI has nowhere to put one.
    try:
        from ark.engines.sdk_runtime import sdk_runtime_enabled
        on_sdk = sdk_runtime_enabled()
    except Exception:
        on_sdk = os.environ.get("ARK_AGENT_RUNTIME", "cli").strip().lower() == "sdk"
    if not on_sdk:
        return False, "requires ARK_AGENT_RUNTIME=sdk"
    if not apptainer_bin():
        return False, "apptainer is not installed on this node"
    sif = agent_server_sif()
    if not sif.is_file():
        return False, (f"agent-server image missing at {sif} — pull it once with: "
                       f"apptainer pull {sif} docker://{AGENT_SERVER_IMAGE}")
    return True, f"agent-server image {sif}"


def structural_sandbox_config(code_dir) -> Optional[dict]:
    """Workspace settings for ``sdk_driver.py``, or None when the lane is off.

    Built here rather than in the driver because the driver runs under the
    OpenHands interpreter and imports nothing from ark.
    """
    if not structural_sandbox_status()[0]:
        return None
    code_dir = Path(code_dir).resolve()
    return {
        "kind": "apptainer",
        "sif_file": str(agent_server_sif()),
        # Bind the project at its OWN host path, not at the container's default
        # /workspace. A conda env is not relocatable — its shebangs and
        # sys.prefix are absolute — so `.conda_env/bin/python` only survives if
        # the project appears inside the container at the path it was built at.
        # Same trick the advisory helper has always used (`--bind $PROJ:$PROJ`).
        "bind": f"{code_dir}:{code_dir}",
        "working_dir": str(code_dir),
    }


_HELPER = r'''#!/bin/bash
# ARK experiment sandbox — AUTO-GENERATED by ark.sandbox. Runs a command inside
# the Apptainer base image with the project dir bind-mounted, isolated from the
# host $HOME. Fail-open: if apptainer/image is missing, runs on the host so a run
# never breaks (best-effort isolation).
set -uo pipefail
SIF="${ARK_SANDBOX_SIF:-__SIF__}"
APPTAINER="__APPTAINER__"
if command -v apptainer >/dev/null 2>&1; then APPTAINER="$(command -v apptainer)"; fi
PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$#" -eq 0 ]; then echo "usage: sandbox/run.sh <command> [args...]" >&2; exit 2; fi
if [ ! -x "$APPTAINER" ] || [ ! -f "$SIF" ]; then
  echo "[sandbox] apptainer/image unavailable — running on host (NO isolation)" >&2
  exec "$@"
fi
# Persist extra pip installs across invocations via a host-side, bind-mounted dir.
mkdir -p "$PROJ/sandbox/pydeps"
export APPTAINERENV_PYTHONPATH="$PROJ/sandbox/pydeps:${PYTHONPATH:-}"
exec "$APPTAINER" exec --writable-tmpfs --no-home \
  --bind "$PROJ:$PROJ" --pwd "$PWD" \
  "$SIF" "$@"
'''


def write_sandbox_helper(code_dir) -> Optional[Path]:
    """Seed <code_dir>/sandbox/run.sh. Returns the path, or None if unavailable."""
    # Under the structural lane the agent is already inside the container, where
    # there is no apptainer binary to nest a second one with. The helper would
    # be a trap: every invocation would hit its fail-open branch and quietly
    # announce "NO isolation" on a run that is, in fact, isolated.
    if structural_sandbox_status()[0]:
        return None
    if not sandbox_available():
        return None
    code_dir = Path(code_dir)
    sdir = code_dir / "sandbox"
    sdir.mkdir(parents=True, exist_ok=True)
    helper = sdir / "run.sh"
    helper.write_text(
        _HELPER.replace("__SIF__", sandbox_sif_path()).replace("__APPTAINER__", apptainer_bin() or APPTAINER_FALLBACK)
    )
    helper.chmod(0o755)
    return helper


def experimenter_directive() -> str:
    """System-rule block mandating sandboxed experiment execution (empty if N/A)."""
    if structural_sandbox_status()[0]:
        # Nothing to mandate — the confinement no longer depends on the agent
        # agreeing to it. Repeating the advisory text here would be actively
        # harmful: it would send the agent looking for a ./sandbox/run.sh that
        # is deliberately absent. All this block does now is explain the
        # surroundings, so the agent does not misread a missing host tool as a
        # broken environment.
        return (
            "\n\n## Environment — you are inside a container\n"
            "Your shell and file tools run inside an Apptainer container, not on "
            "the host. The project directory is mounted at its usual path and is "
            "writable: files you write to results/ or paper/ land on the host "
            "normally. Run experiments the normal way (e.g. "
            "`.conda_env/bin/python your_experiment.py`) — do NOT wrap commands "
            "in any sandbox helper. Host-only tools (Slurm, host conda) are not "
            "reachable from in here; if you need one, report it rather than "
            "working around it.\n"
        )
    if not sandbox_available():
        return ""
    return (
        "\n\n## CRITICAL RULES — Experiment Sandbox (Apptainer)\n"
        "Run ALL experiment code inside the project's Apptainer sandbox, NOT on the "
        "bare host. Take your normal project-env command and prefix it with the helper:\n"
        "    ./sandbox/run.sh .conda_env/bin/python your_experiment.py\n"
        "    ./sandbox/run.sh bash your_script.sh\n"
        "This runs the project's conda env (`.conda_env`, with ALL its installed "
        "packages) INSIDE the container — you keep every project dependency AND get "
        "host isolation, because `.conda_env` is bind-mounted at the same path. "
        "Install extra packages the normal way (into `.conda_env`); they are visible "
        "inside the sandbox automatically. Files written to results/ or the workspace "
        "persist on the host. Do NOT run experiment code directly on the host (e.g. "
        "bare `.conda_env/bin/python exp.py`) — ALWAYS wrap it with ./sandbox/run.sh.\n"
    )
