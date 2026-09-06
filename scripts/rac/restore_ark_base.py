"""Return the shared conda env to the package set it had before the runs.

The three projects share one env (`.conda_env` is a symlink to `ark-base`), so
every `pip install` an experimenter agent runs lands in it: 143 packages before
the batch, 179 during. Two kinds of damage matter — packages added, and
packages whose version an install changed underneath the originals (that is how
`huggingface_hub` went from 0.26.5 to 1.30.0 and broke `transformers`).

Compare on PEP 503-normalised names: pip prints both `huggingface-hub` and
`huggingface_hub` depending on the metadata, so a raw diff reports a package as
removed and added rather than changed.

    python scripts/rac/restore_ark_base.py                 # show the plan
    python scripts/rac/restore_ark_base.py --apply         # do it

Run it only when no run is live: pip rewriting site-packages under a working
agent is its own failure mode.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
BEFORE = HERE / "ark_base_BEFORE.txt"
PYTHON = "/home/luoy0a/anaconda3/envs/ark-base/bin/python"


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse(lines: list[str]) -> dict[str, str]:
    versions = {}
    for line in lines:
        line = line.strip()
        if not line or "==" not in line:
            continue
        name, _, version = line.partition("==")
        versions[normalise(name)] = version
    return versions


def current() -> dict[str, str]:
    out = subprocess.run([PYTHON, "-m", "pip", "list", "--format=freeze"],
                         capture_output=True, text=True, check=True).stdout
    return parse(out.splitlines())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    before, now = parse(BEFORE.read_text().splitlines()), current()
    added = sorted(set(now) - set(before))
    changed = sorted(name for name in set(now) & set(before) if now[name] != before[name])
    missing = sorted(set(before) - set(now))

    print(f"before {len(before)} packages, now {len(now)}")
    print(f"  added   {len(added)}: {', '.join(added) or '-'}")
    print(f"  changed {len(changed)}: " +
          (", ".join(f"{n} {before[n]}→{now[n]}" for n in changed) or "-"))
    print(f"  missing {len(missing)}: " +
          (", ".join(f"{n}=={before[n]}" for n in missing) or "-"))
    if not (added or changed or missing):
        print("nothing to restore")
        return 0
    if not args.apply:
        print("\nre-run with --apply to uninstall what was added and pin the rest back")
        return 0

    # Order matters: drop the additions first, so restoring a pinned version is
    # not immediately re-broken by a dependency that is about to be removed.
    if added:
        subprocess.run([PYTHON, "-m", "pip", "uninstall", "-y", *added], check=False)
    pins = [f"{name}=={before[name]}" for name in changed + missing]
    if pins:
        subprocess.run([PYTHON, "-m", "pip", "install", "--no-deps", *pins], check=False)

    after = current()
    still_added = sorted(set(after) - set(before))
    still_off = sorted(n for n in set(after) & set(before) if after[n] != before[n])
    still_missing = sorted(set(before) - set(after))
    print(f"\nafter: {len(after)} packages; extra={len(still_added)} "
          f"wrong-version={len(still_off)} missing={len(still_missing)}")
    for label, names in (("extra", still_added), ("wrong-version", still_off),
                         ("missing", still_missing)):
        if names:
            print(f"  {label}: {', '.join(names)}")
    return 0 if not (still_added or still_off or still_missing) else 1


if __name__ == "__main__":
    sys.exit(main())
