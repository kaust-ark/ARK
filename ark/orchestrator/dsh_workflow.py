"""Run the hosted workflow as a Cordis plugin of the pinned DSH runtime."""

import fcntl
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading

from ark.engines.cli import selected_agent_runtime
from ark.engines.dsh_runtime import SDK_VERSION, dsh_interpreter
from ark.workflow import WorkflowSteps


def hosted_workflow_enabled(project_id):
    choice = os.environ.get("ARK_WORKFLOW_RUNTIME", "").strip().lower()
    if choice not in ("", "python", "dsh", "native", "legacy"):
        raise ValueError("ARK_WORKFLOW_RUNTIME must be native, legacy, dsh, or python")
    if selected_agent_runtime() != "dsh" or choice == "python":
        return False
    return choice in ("dsh", "native", "legacy") or bool(project_id)


def _run_legacy_workflow(project):
    steps = WorkflowSteps(project)
    with tempfile.TemporaryDirectory(prefix="idea2paper-workflow-") as directory:
        root = Path(directory)
        plugin = root / "workflow.mjs"
        plugin.write_bytes(Path(__file__).with_suffix(".mjs").read_bytes())
        profile = root / "profiles" / "idea2paper-hosted"
        profile.mkdir(parents=True)
        (profile / "package.json").write_text(json.dumps({
            "name": "dsh-profile-idea2paper-hosted", "private": True,
            "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base"]}},
        }))
        (profile / "cordis.patch.yml").write_text(json.dumps([
            {"id": name, "disabled": True} for name in (
                "hmr", "session-log-deepseek", "plugin-package-inventory-deepseek",
                "session-telemetry-otel", "deepseek-account", "llm-deepseek-account",
            )
        ] + [{"insert": [{"id": "idea2paper-hosted", "name": str(plugin)}]}]))
        parent, child = socket.socketpair()
        with parent, child:
            env = {name: os.environ[name] for name in ("PATH", "HOME", "LANG", "TMPDIR", "XDG_CACHE_HOME")
                   if name in os.environ}
            env.update(DSH_HOME=directory, DSH_TELEMETRY_DISABLED="1", ARK_DSH_WORKFLOW_FD=str(child.fileno()))
            env["PKG_NATIVE_CACHE_PATH"] = os.environ.get("ARK_DSH_PKG_CACHE") or str(
                Path.home() / ".cache" / "idea2paper" / f"dsh-pkg-{SDK_VERSION}")
            proc = subprocess.Popen(
                [dsh_interpreter(), "-c", "from deepseek_harness_runtime import main; main()",
                 "--profile", "idea2paper-hosted"],
                cwd=directory, env=env, stdin=subprocess.DEVNULL, pass_fds=(child.fileno(),),
            )
            child.close()
            try:
                with parent.makefile("rb") as requests:
                    parent.settimeout(45)
                    if requests.readline(512) != b"ready-v2\n":
                        raise RuntimeError("DSH workflow did not start")
                    project.log("DSH hosted workflow started", "INFO")
                    while True:
                        line = requests.readline(512)
                        if line == b"done\n":
                            break
                        action = line.decode("ascii").rstrip("\n")
                        if action not in steps.actions:
                            raise RuntimeError("DSH workflow disconnected or sent an unknown step")
                        outcome = steps.actions[action]()
                        parent.sendall(
                            (json.dumps(outcome, allow_nan=False) + "\n").encode()
                        )
                if proc.wait(timeout=20) != 0:
                    raise RuntimeError("DSH workflow exited with an error")
            finally:
                parent.close()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
    return steps.iterations_at_start


def uses_native_project(project):
    """New projects use the shared bundle; a checkpoint retains its original runner."""
    choice = os.getenv("ARK_WORKFLOW_RUNTIME", "").lower().strip()
    if choice in ("python", "legacy"):
        return False
    state = getattr(project, "state_dir", None)
    if state is None:
        return False
    checkpoint = Path(project.code_dir) / "workflow.json"
    if checkpoint.is_file():
        return json.loads(checkpoint.read_text()).get("version") == 3
    return not any(
        (state / name).is_file()
        for name in (
            "checkpoint.yaml",
            "dev_phase_state.yaml",
            "paper_state.yaml",
            "research_pipeline_state.yaml",
        )
    )


def native_interpreter(project):
    """Use an explicitly selected DSH runtime or the project's compatible isolated environment."""
    configured = os.environ.get("ARK_DSH_PYTHON")
    if configured:
        return configured
    candidate = Path(project.code_dir) / ".env/bin/python"
    if candidate.is_file():
        from ark.engines.dsh_runtime import dsh_installation_error
        if not dsh_installation_error(str(candidate)):
            return str(candidate)
    return dsh_interpreter()


def _bundle_archive():
    provided = os.getenv("IDEA2PAPER_BUNDLE_PATH")
    if provided:
        return Path(provided).resolve(strict=True)
    source = Path(__file__).resolve().parents[2]
    pack = source / "integrations/dsh/pack.py"
    if not pack.is_file():
        raise RuntimeError(
            "Set IDEA2PAPER_BUNDLE_PATH to the tested Idea2Paper bundle tarball."
        )
    # Include dirty release sources. Two provisioners can share the same content-addressed build.
    inputs = [
        source / "pyproject.toml",
        *sorted((source / "integrations/dsh").glob("*.js")),
        *sorted((source / "integrations/dsh").glob("*.py")),
        source / "integrations/dsh/package.json",
        source / "integrations/dsh/cordis.patch.yml",
        *sorted((source / "ark").rglob("*.py")),
        *sorted((source / "ark/templates").rglob("*.prompt")),
    ]
    digest = hashlib.sha256()
    for path in inputs:
        digest.update(
            str(path.relative_to(source)).encode() + b"\0" + path.read_bytes()
        )
    for name in ("skills", "venue_templates"):
        for path in sorted((source / name).rglob("*")):
            if path.is_file():
                digest.update(
                    str(path.relative_to(source)).encode() + b"\0" + path.read_bytes()
                )
    cache = (
        Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "idea2paper/bundles"
        / digest.hexdigest()
    )
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (cache / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        archives = list(cache.glob("idea2paper-dsh-*.tgz"))
        if not archives:
            subprocess.run(
                [sys.executable, str(pack), "--out", str(cache)], check=True, cwd=source
            )
            archives = list(cache.glob("idea2paper-dsh-*.tgz"))
        if len(archives) != 1:
            raise RuntimeError("The bundle build must produce one Idea2Paper tarball.")
        return archives[0]


def _install_bundle(profile):
    """Install the immutable, dependency-bundled npm payload without a registry request."""
    marker = profile / "bundle.sha256"
    package = profile / "node_modules/idea2paper-dsh"
    required_digest = os.getenv("IDEA2PAPER_BUNDLE_SHA256")
    if marker.is_file() and package.is_dir():
        if required_digest and marker.read_text().strip() != required_digest:
            raise RuntimeError(
                "Existing project is pinned to a different Idea2Paper bundle."
            )
        return marker.read_text().strip()
    archive = _bundle_archive()
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if required_digest and required_digest != digest:
        raise RuntimeError("Idea2Paper bundle checksum did not match.")
    package.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=profile) as staging:
        with tarfile.open(archive, "r:gz") as packed:
            for member in packed.getmembers():
                path = Path(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or path.parts[:1] != ("package",)
                ):
                    raise ValueError("Invalid path in the Idea2Paper bundle.")
                if not (member.isdir() or member.isfile()):
                    raise ValueError("Unsupported npm tar entry type.")
                target = Path(staging) / path
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as dest, packed.extractfile(member) as src:
                        shutil.copyfileobj(src, dest)
        unpacked = Path(staging) / "package"
        info = json.loads((unpacked / "package.json").read_text())
        if (
            info.get("name") != "idea2paper-dsh"
            or info.get("engines", {}).get("dsh") != "0.2.1-rc.2"
        ):
            raise ValueError("This bundle does not target the installed DSH runtime.")
        if not (unpacked / "node_modules/commander/package.json").is_file():
            raise ValueError("The hosted tarball must bundle its commander dependency.")
        if package.exists():
            # The directory is installed atomically before the marker. Recover only
            # if a crash left an intact byte-for-byte copy of the selected package.
            for item in unpacked.rglob("*"):
                if (
                    item.is_file()
                    and (package / item.relative_to(unpacked)).read_bytes()
                    != item.read_bytes()
                ):
                    raise ValueError(
                        "Incomplete bundle installation. Restore the pinned archive."
                    )
        else:
            unpacked.rename(package)
    from ark.orchestrator.state import _atomic_write_text

    _atomic_write_text(marker, digest + "\n")
    return digest


def _native_profile(project, root, profile):
    from ark.engines.dsh_profile import role_patch
    from ark.engines.dsh_runtime import (
        DSHRuntime,
        _history_budget,
        _max_tokens,
        _window,
        route,
        supports_images,
    )
    from ark.orchestrator.state import _atomic_write_text

    model = project.config.get("model_variant") or project.model
    protocol, endpoint = route(model)
    prefix, _, model_id = model.partition("/")
    provider = (
        ("google" if protocol == "google-generative-ai" else "ark-gateway")
        if protocol
        else "deepseek-official"
    )
    window = _window(model)
    maximum = _max_tokens(model)
    patch = role_patch(
        role="project researcher",
        workdir=root,
        provider=provider,
        model_id=model_id,
        window=window,
        max_tokens=maximum,
        history_budget=_history_budget(model, window),
        api=protocol,
        endpoint=endpoint,
        skills_dir=root / ".claude/skills",
        model_provider=prefix,
        image_input=supports_images(model),
        retries=int(os.getenv("ARK_DSH_RETRIES", "0")),
    )
    # The full base bundle already mounts pi-ai. The SDK profile adds it on demand.
    for idx, row in enumerate(patch):
        if row.get("insert", [{}])[0].get("id") == "llm-pi-ai":
            patch[idx] = {"id": "llm-pi-ai", "config": row["insert"][0]["config"]}
        if row.get("id") == "llm-deepseek" and "config" in row and endpoint:
            row["config"]["baseURL"] = endpoint
    patch.append(
        {
            "id": "agent-default-model",
            "config": {"provider": provider, "model": model_id},
        }
    )
    signature = {
        "version": SDK_VERSION,
        "model": model,
        "protocol": protocol,
        "endpoint": endpoint,
    }
    manifest = profile / "route.json"
    if manifest.is_file() and json.loads(manifest.read_text()) != signature:
        raise RuntimeError(
            "Native owner resume requires the original model and endpoint."
        )
    _atomic_write_text(manifest, json.dumps(signature))
    _atomic_write_text(
        profile / "package.json",
        json.dumps(
            {
                "name": "dsh-profile-idea2paper",
                "private": True,
                "dsh": {
                    "profile": {"bundles": ["@deepseek-ai/dsh-base", "idea2paper-dsh"]}
                },
            }
        ),
    )
    _atomic_write_text(profile / "cordis.yml", "[]\n")
    _atomic_write_text(profile / "cordis.patch.yml", json.dumps(patch))
    env = DSHRuntime(model).build_env(root)
    if not protocol and env.get("ARK_DSH_API_KEY"):
        env["DEEPSEEK_API_KEY"] = env["ARK_DSH_API_KEY"]
    if not protocol and endpoint:
        env["DEEPSEEK_BASE_URL"] = endpoint
    env.update(
        IDEA2PAPER_HOSTED="1",
        DSH_HOME=str(profile.parent.parent),
        DSH_TELEMETRY_DISABLED="1",
    )
    return env


def native_delivery_current(project):
    from ark import review
    from ark.orchestrator.native_domain import NativeProject

    domain = NativeProject(Path(project.code_dir), host=project)
    if domain.state.get("state") != "delivered":
        return False
    try:
        domain._check_compiled()
        record = json.loads((project.state_dir / "native_review.json").read_text())
        snapshot = domain._snapshot()
    except (OSError, ValueError):
        return False
    return bool(
        record.get("deliverable")
        and domain.state.get("delivery_revision") == snapshot["revision"]
        and review.record_is_current(snapshot, record)
    )


def run_native_project(project, *, message=None):
    from ark.orchestrator.native_domain import NativeProject, serve

    project._native_owner = True
    project._native_commands = {}
    if hasattr(project, "cp"):
        from ark.controlplane.local_db import LocalDbControlPlaneClient

        if isinstance(project.cp, LocalDbControlPlaneClient):
            project.cp.defer_command_ack = True
    root = Path(project.code_dir).resolve()
    home = root / ".dsh/native/home"
    if not home.resolve().is_relative_to(root):
        raise ValueError("DSH state directory escapes the project workspace.")
    profile = home / "profiles/idea2paper"
    profile.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (home.parent / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "The native project owner already has an active worker."
            ) from exc
        digest = _install_bundle(profile)
        env = _native_profile(project, root, profile)
        if getattr(project, "max_end_time", None) and not message:
            remaining = int(
                (project.max_end_time - datetime.now()).total_seconds() * 1000
            )
            if remaining <= 0:
                raise RuntimeError(
                    "Project runtime budget expired before the native owner started."
                )
            env["IDEA2PAPER_OWNER_TIMEOUT_MS"] = str(min(remaining, 2_147_483_647))
        domain = NativeProject(root, host=project)
        project._native_domain = domain
        if not message and hasattr(project, "start_telegram_listener"):
            project.start_telegram_listener()
            project._ensure_control_poller()
        project._native_provider_error = None
        command = (
            ["resume", str(root)]
            if (root / "workflow.json").is_file()
            else ["run", str(root), project._research_idea]
        )
        if message:
            if command[0] != "resume":
                raise RuntimeError("The project owner has not been created yet.")
            command.append(message)
        project.log(
            f"Running native Idea2Paper bundle {digest[:12]} via DSH {SDK_VERSION}",
            "INFO",
        )
        parent, child = socket.socketpair()
        with parent, child:
            env["IDEA2PAPER_DOMAIN_FD"] = str(child.fileno())
            proc = subprocess.Popen(
                [
                    native_interpreter(project),
                    "-I",
                    "-c",
                    "from deepseek_harness_runtime import main; main()",
                    "--profile",
                    "idea2paper",
                    *command,
                ],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                pass_fds=(child.fileno(),),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
            child.close()
            output = []

            def relay():
                for line in proc.stdout:
                    value = line.rstrip()
                    if value:
                        output[:] = (output + [value])[-3:]
                        project.log(value[:800], "INFO")

            reader = threading.Thread(target=relay, daemon=True)
            reader.start()
            try:
                with (
                    parent.makefile("r", encoding="utf-8") as incoming,
                    parent.makefile("w", encoding="utf-8") as outgoing,
                ):
                    serve(domain, incoming, outgoing)
                rc = proc.wait(timeout=20)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                reader.join(timeout=3)
                proc.stdout.close()
        if project._stop_requested:
            return
        if rc:
            provider_error = getattr(project, "_native_provider_error", None)
            if provider_error and not message:
                # The control-plane terminal sweep owns the user-facing account
                # reminder. A generic pipeline failure notification would duplicate it.
                project._terminal_error = provider_error
                return
            raise RuntimeError(provider_error or project._terminal_error or
                               ("Native owner failed: " + " ".join(output)[-500:]))
        if getattr(project, "_paused", False):
            return
        if not message and not native_delivery_current(project):
            raise RuntimeError(
                "The owner stopped without a matching verified delivery and independent review."
            )


def run_workflow(project):
    if not uses_native_project(project):
        return _run_legacy_workflow(project)
    project._native_owner = True
    while True:
        run_native_project(project)
        if not getattr(project, "_paused", False) or project._stop_requested:
            break
        project._maybe_park()
        if project._stop_requested:
            break
    return None
