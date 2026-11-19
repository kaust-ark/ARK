"""Start the exact companion supplied by this DSH bundle, installing it once."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import venv


def ready_python(cache, version, digest):
    # The unsuffixed path is also the cache used by previous bundle versions.
    for target in (cache / version, *sorted(cache.glob(version + "-*"))):
        try:
            if (target / ".ready").read_text() == digest and (target / "bin/python").is_file():
                return target / "bin/python"
        except FileNotFoundError:
            continue
    return None


def install(cache, version, wheel, digest):
    with (cache / (version + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if ready_python(cache, version, digest):
            return
        target = cache / version
        try:
            target.mkdir(mode=0o700)
        except FileExistsError:
            # An interrupted older bootstrap may have left pip writing at its
            # absolute prefix. Give this install its own permanent path.
            target = Path(tempfile.mkdtemp(dir=cache, prefix=version + "-"))
        python = target / "bin/python"
        try:
            venv.EnvBuilder(with_pip=True).create(target)
            subprocess.run(
                [str(python), "-I", "-m", "pip", "install", "--disable-pip-version-check",
                 str(wheel), "google-genai>=2,<3"],
                check=True, stdout=sys.stderr,
            )
            subprocess.run([str(python), "-I", "-c", "import ark.orchestrator.native_domain, google.genai"],
                           check=True, stdout=sys.stderr)
            pending = target / ".ready.tmp"
            pending.write_text(digest)
            pending.replace(target / ".ready")
        except Exception:
            # This worker allocated the prefix and has waited for its own children.
            shutil.rmtree(target, ignore_errors=True)
            raise


def main():
    if sys.version_info < (3, 10):
        raise RuntimeError("Idea2Paper requires Python 3.10 or later.")
    directory = Path(__file__).resolve().parent / "companion"
    manifest = json.loads((directory / "manifest.json").read_text())
    wheel = directory / manifest["wheel"]
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if wheel.parent != directory or digest != manifest["sha256"]:
        raise RuntimeError("The installed Idea2Paper companion failed its integrity check.")
    cache_root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache").absolute()
    cache = cache_root / "idea2paper" / "companions"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    version = f"py{sys.version_info.major}{sys.version_info.minor}-{digest}"
    if sys.argv[1:] == ["--install"]:
        install(cache, version, wheel, digest)
        return
    python = ready_python(cache, version, digest)
    if python is None:
        print("Setting up the Idea2Paper Python companion (first run).", file=sys.stderr, flush=True)
        # The installer owns the flock and its own session. Losing a dashboard or
        # CLI connection cannot release the lock while venv or pip is writing.
        with tempfile.TemporaryFile() as output:
            worker = subprocess.run([sys.executable, "-I", __file__, "--install"], stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=output, start_new_session=True)
            if worker.returncode:
                output.seek(max(0, output.tell() - 4096))
                raise RuntimeError("Idea2Paper companion setup failed:\n" + output.read().decode(errors="replace"))
        python = ready_python(cache, version, digest)
        if python is None:
            raise RuntimeError("Companion installer exited without publishing a ready environment.")
    os.execv(str(python), [str(python), "-I", "-u", "-m", "ark.orchestrator.native_domain"])


if __name__ == "__main__":
    main()
