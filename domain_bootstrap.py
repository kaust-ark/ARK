"""Start the exact companion supplied by this DSH bundle, installing it once."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv


def main():
    if sys.version_info < (3, 10):
        raise RuntimeError("Idea2Paper requires Python 3.10 or later.")
    directory = Path(__file__).resolve().parent / "companion"
    manifest = json.loads((directory / "manifest.json").read_text())
    wheel = directory / manifest["wheel"]
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if wheel.parent != directory or digest != manifest["sha256"]:
        raise RuntimeError("The installed Idea2Paper companion failed its integrity check.")
    cache_root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    cache = cache_root / "idea2paper" / "companions"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    version = f"py{sys.version_info.major}{sys.version_info.minor}-{digest}"
    target = cache / version
    python = target / "bin/python"
    with (cache / (version + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (target / ".ready").is_file():
            # The venv stays at its final path so generated entry points have valid shebangs.
            if target.exists():
                shutil.rmtree(target)
            print("Setting up the Idea2Paper Python companion (first run).", file=sys.stderr, flush=True)
            venv.EnvBuilder(with_pip=True).create(target)
            subprocess.run(
                [
                    str(python),
                    "-I",
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    str(wheel),
                    "google-genai>=2,<3",
                ],
                check=True,
                stdout=sys.stderr,
            )
            subprocess.run(
                [str(python), "-I", "-c", "import ark.orchestrator.native_domain, google.genai"],
                check=True,
                stdout=sys.stderr,
            )
            (target / ".ready").write_text(digest)
    os.execv(str(python), [str(python), "-I", "-u", "-m", "ark.orchestrator.native_domain"])


if __name__ == "__main__":
    main()
