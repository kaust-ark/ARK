"""Start a hosted project with the Python code shipped beside its DSH plugin."""

import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import zipfile


def _archive_for_project(profile, root):
    from ark.orchestrator.dsh_workflow import _bundle_archive

    cache = Path(os.getenv("XDG_CACHE_HOME") or Path.home() / ".cache") / "idea2paper"
    if cache.resolve().is_relative_to(root):
        raise RuntimeError("The hosted bundle cache must be outside the research workspace.")
    archives = cache / "hosted-archives"
    archives.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = profile / "bundle.sha256"
    pin = marker.read_text().strip() if marker.is_file() else None
    if pin is not None and not re.fullmatch("[0-9a-f]{64}", pin):
        raise ValueError("Invalid hosted project bundle digest.")
    if pin and (stored := archives / (pin + ".tgz")).is_file():
        if hashlib.sha256(stored.read_bytes()).hexdigest() == pin:
            return stored, pin

    # Older releases cached a built tarball by its source digest. A project
    # pin selects trusted release bytes, never executable files from an upload.
    required = pin or os.environ.get("IDEA2PAPER_BUNDLE_SHA256")
    candidates = []
    try:
        candidates.append(_bundle_archive())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        if not pin:
            raise
        original_error = error
    else:
        original_error = None
    if pin:
        candidates.extend((cache / "bundles").glob("*/*.tgz"))
    for candidate in candidates:
        data = candidate.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if required and digest != required:
            continue
        stored = archives / (digest + ".tgz")
        if not stored.is_file() or hashlib.sha256(stored.read_bytes()).hexdigest() != digest:
            with tempfile.NamedTemporaryFile(dir=archives, delete=False) as output:
                try:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                    os.replace(output.name, stored)
                finally:
                    Path(output.name).unlink(missing_ok=True)
        return stored, digest
    raise RuntimeError(f"Restore the trusted Idea2Paper release archive {required} to resume this project.") from original_error


def boot_native_worker(project_id, code_dir, project_dir):
    from ark.orchestrator.dsh_workflow import _install_bundle, hosted_workflow_enabled, uses_native_project

    if not project_id or not code_dir or not project_dir:
        return
    root = Path(code_dir).resolve()
    project = SimpleNamespace(code_dir=root, state_dir=root / "auto_research/state", _project_id=project_id)
    if not hosted_workflow_enabled(project_id, project) or not uses_native_project(project):
        return
    profile = root / ".dsh/native/home/profiles/idea2paper"
    if profile.resolve() != profile:
        raise ValueError("Invalid linked DSH profile in the project workspace.")
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (profile / ".bundle.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        archive, digest = _archive_for_project(profile, root)
        _install_bundle(profile, archive=archive)
    # exec preserves the scheduler PID and signal ownership. No research,
    # session, control-plane status or other project side effects precede it.
    env = {**os.environ, "IDEA2PAPER_BUNDLE_PATH": str(archive), "IDEA2PAPER_BUNDLE_SHA256": digest}
    os.execve(sys.executable, [sys.executable, str(Path(__file__).resolve()), str(archive), digest, *sys.argv[1:]], env)


def run_pinned_worker(archive, digest):
    data = Path(archive).read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError("Hosted bundle changed after project selection.")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as bundle:
        info = json.load(bundle.extractfile("package/companion/manifest.json"))
        wheel = info["wheel"]
        if Path(wheel).name != wheel or not wheel.endswith(".whl"):
            raise ValueError("Invalid companion wheel name.")
        wheel_data = bundle.extractfile("package/companion/" + wheel).read()
    if hashlib.sha256(wheel_data).hexdigest() != info["sha256"]:
        raise RuntimeError("Hosted Python companion checksum mismatch.")
    # The project's conda environment supplies site dependencies. The wheel is
    # pure Python and includes the scientific assets that need real file paths.
    with tempfile.TemporaryDirectory(prefix="idea2paper-worker-") as source:
        with zipfile.ZipFile(io.BytesIO(wheel_data)) as distribution:
            for name in distribution.namelist():
                if Path(name).is_absolute() or ".." in Path(name).parts:
                    raise ValueError("Invalid companion member path.")
            distribution.extractall(source)
        if not (Path(source) / "ark/orchestrator/core.py").is_file():
            raise RuntimeError("Hosted companion does not contain the project worker.")
        sys.path.insert(0, source)
        os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, (source, os.getenv("PYTHONPATH"))))
        from ark.orchestrator.core import main

        if Path(main.__code__.co_filename).resolve() != Path(source) / "ark/orchestrator/core.py":
            raise RuntimeError("The worker imported a different Idea2Paper runtime from its pinned wheel.")
        print(f"[Idea2Paper] Hosted bundle {digest}, Python wheel {info['sha256']}", flush=True)
        main()


if __name__ == "__main__":
    archive, digest = sys.argv[1:3]
    del sys.argv[1:3]
    run_pinned_worker(archive, digest)
