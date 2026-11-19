"""Build the public bundle with a matching, self-contained first-party wheel."""

import argparse
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


def build(out):
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info", ".DS_Store", "node_modules")
    env = dict(os.environ, SOURCE_DATE_EPOCH="1609459200", PYTHONHASHSEED="0")
    with tempfile.TemporaryDirectory(prefix="idea2paper-pack-") as tmp:
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
        result = subprocess.run(
            ["npm", "pack", "--ignore-scripts", "--json", "--pack-destination", str(out)],
            cwd=pkg,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        filename = json.loads(result.stdout)[0]["filename"]
    return out / filename


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    print(build(parser.parse_args().out), flush=True)
