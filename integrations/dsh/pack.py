"""Build the public bundle with a matching, self-contained first-party wheel."""

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


def _staged_digest(staging):
    """Key the cache on the exact npm and wheel inputs, including the build recipe."""
    digest = hashlib.sha256(Path(__file__).read_bytes())
    for path in sorted(staging.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(staging).as_posix().encode() + b"\0")
            digest.update((path.stat().st_mode & 0o777).to_bytes(2, "big"))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def build(out, *, cached=False):
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    skip = shutil.ignore_patterns("__pycache__", "*.py[co]", "*.egg-info", ".DS_Store", "node_modules",
                                  ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git")
    env = dict(os.environ, SOURCE_DATE_EPOCH="1609459200", PYTHONHASHSEED="0")
    with tempfile.TemporaryDirectory(prefix="idea2paper-pack-") as tmp, ExitStack() as stack:
        pkg = Path(tmp) / "package"
        src = Path(tmp) / "wheel-source"
        pkg.mkdir()
        src.mkdir()
        meta = json.loads((HERE / "package.json").read_text())
        for filename in ["LICENSE", "README.md", "package.json", *meta["files"]]:
            if filename != "companion":
                shutil.copy2(HERE / filename, pkg / filename)
        for folder in ("ark", "website"):
            shutil.copytree(REPO / folder, src / folder, ignore=skip)
        for filename in ("pyproject.toml", "LICENSE"):
            shutil.copy2(REPO / filename, src / filename)
        assets = src / "ark/bundle_assets"
        for folder in ("skills", "venue_templates"):
            shutil.copytree(REPO / folder, assets / folder, ignore=skip)
        if cached:
            import fcntl

            out = out / _staged_digest(Path(tmp))
            out.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock = stack.enter_context((out / ".lock").open("a"))
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            archives = list(out.glob("idea2paper-dsh-*.tgz"))
            if len(archives) > 1:
                raise RuntimeError("The bundle cache must contain one Idea2Paper tarball.")
            if archives:
                return archives[0]
        companion = pkg / "companion"
        companion.mkdir()
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "-q",
                "--wheel-dir",
                str(companion),
                str(src),
            ],
            check=True,
            env=env,
            stdout=sys.stderr,
        )
        (wheel,) = companion.glob("*.whl")
        info = {"wheel": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
        (companion / "manifest.json").write_text(json.dumps(info) + "\n")
        subprocess.run(
            [
                "npm",
                "install",
                "--ignore-scripts",
                "--no-package-lock",
                "--no-audit",
                "--no-fund",
                "--omit=dev",
                "--legacy-peer-deps",
            ],
            cwd=pkg,
            env=env,
            check=True,
            stdout=sys.stderr,
        )
        # Only publish a finished tarball. A failed or interrupted npm pack
        # cannot leave a partial archive where another worker would reuse it.
        destination = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=out)))
        result = subprocess.run(
            ["npm", "pack", "--ignore-scripts", "--json", "--pack-destination", str(destination)],
            cwd=pkg,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        filename = json.loads(result.stdout)[0]["filename"]
        os.replace(destination / filename, out / filename)
    return out / filename


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--out", type=Path)
    destination.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    print(build(args.cache_dir or args.out, cached=args.cache_dir is not None), flush=True)
