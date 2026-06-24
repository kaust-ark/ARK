#!/usr/bin/env python3
"""Refresh the model-picker slugs from the live OpenRouter catalog.

The dashboard's model picker (website/dashboard/templates/app.html) and the
OpenRouter routing map (website/dashboard/routes.py `_OPENROUTER_SLUG`) hardcode
a slug per model family (e.g. ``openrouter/minimax/minimax-m3``). Those go stale
as vendors ship new versions. This script pulls https://openrouter.ai/api/v1/models,
finds the latest STABLE version per tracked family, and either reports the drift
(``--check``, default) or patches the files in place (``--apply``).

"Stable" = the slug matches the family's exact ``base-<version>[suffix]`` shape
with the highest numeric version. Preview / experimental / dated / image / audio
/ ``-fast`` / ``-thinking`` variants are skipped — they share a family stem but
are not drop-in replacements, and surfacing one unattended could break a run.

Designed to be run by a systemd timer every 2 days. ``--check`` is safe to run
unattended (it only reads + reports); ``--apply`` edits source — every change is
git-revertible and logged. The timer runs ``--check`` by default; flip it to
``--apply`` if you want fully hands-off updates.
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import urllib.request
from pathlib import Path

CATALOG_URL = "https://openrouter.ai/api/v1/models"
REPO = Path(__file__).resolve().parent.parent
APP_HTML = REPO / "website" / "dashboard" / "templates" / "app.html"
ROUTES_PY = REPO / "website" / "dashboard" / "routes.py"

# Each tracked family: a regex over catalog ids whose one capture group is the
# numeric version. The latest-stable slug for the family is the matching id with
# the highest version tuple.
TRACKED = {
    "claude-sonnet":  r"^anthropic/claude-sonnet-(\d+(?:\.\d+)?)$",
    "claude-opus":    r"^anthropic/claude-opus-(\d+(?:\.\d+)?)$",
    "claude-haiku":   r"^anthropic/claude-haiku-(\d+(?:\.\d+)?)$",
    "gpt-flagship":   r"^openai/gpt-(\d+(?:\.\d+)?)$",
    "gpt-pro":        r"^openai/gpt-(\d+(?:\.\d+)?)-pro$",
    "gpt-mini":       r"^openai/gpt-(\d+(?:\.\d+)?)-mini$",
    "gemini-flash":   r"^google/gemini-(\d+(?:\.\d+)?)-flash$",
    "gemini-pro":     r"^google/gemini-(\d+(?:\.\d+)?)-pro$",
    "deepseek-pro":   r"^deepseek/deepseek-v(\d+(?:\.\d+)?)-pro$",
    "deepseek-flash": r"^deepseek/deepseek-v(\d+(?:\.\d+)?)-flash$",
    "kimi":           r"^moonshotai/kimi-k(\d+(?:\.\d+)?)$",
    "glm":            r"^z-ai/glm-(\d+(?:\.\d+)?)$",
    "minimax":        r"^minimax/minimax-m(\d+(?:\.\d+)?)$",
    "qwen-plus":      r"^qwen/qwen(\d+(?:\.\d+)?)-plus$",
}

# The family-id shape as it appears in a picker chip's slug. Only the row-1/row-2
# showcase chips (radio inputs, ``name="model" value="openrouter/<id>"``) are
# auto-tracked — NOT the deliberately-curated "more…" dropdown (which lists older
# / variant models on purpose) and NOT routes.py's translation map. Matching is
# anchored to the radio-chip context in apply_to_text(), so dropdown <option>s
# and dash-format native strings (claude-sonnet-4-6) are never touched.
SLUG_IN_FILE = {
    "claude-sonnet":  r"anthropic/claude-sonnet-\d+(?:\.\d+)?",
    "claude-opus":    r"anthropic/claude-opus-\d+(?:\.\d+)?",
    "claude-haiku":   r"anthropic/claude-haiku-\d+(?:\.\d+)?",
    "gpt-flagship":   r"openai/gpt-\d+(?:\.\d+)?",
    "gemini-flash":   r"google/gemini-\d+(?:\.\d+)?-flash",
    "deepseek-pro":   r"deepseek/deepseek-v\d+(?:\.\d+)?-pro",
    "kimi":           r"moonshotai/kimi-k\d+(?:\.\d+)?",
    "glm":            r"z-ai/glm-\d+(?:\.\d+)?",
    "minimax":        r"minimax/minimax-m\d+(?:\.\d+)?",
}


def _vtuple(v: str):
    return tuple(int(x) for x in v.split("."))


def _ssl_context():
    # Prefer certifi's CA bundle (urllib otherwise can't find the system store
    # in some conda envs → CERTIFICATE_VERIFY_FAILED).
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def fetch_catalog() -> set[str]:
    req = urllib.request.Request(CATALOG_URL, headers={"User-Agent": "ark-model-refresh"})
    with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as r:
        data = json.load(r)
    return {m["id"] for m in data.get("data", data)}


def latest_per_family(ids: set[str]) -> dict[str, str]:
    out = {}
    for fam, pat in TRACKED.items():
        rx = re.compile(pat)
        best, best_v = None, None
        for i in ids:
            m = rx.match(i)
            if not m:
                continue
            v = _vtuple(m.group(1))
            if best_v is None or v > best_v:
                best, best_v = i, v
        if best:
            out[fam] = best
    return out


def apply_to_text(text: str, latest: dict[str, str]) -> tuple[str, list[str]]:
    """Rewrite only the showcase radio chips: name="model" value="openrouter/<id>".
    The id is captured anchored between that prefix and the closing quote, so
    dropdown <option>s and native dash-format strings are never matched."""
    changes = []
    for fam, find in SLUG_IN_FILE.items():
        new_id = latest.get(fam)
        if not new_id:
            continue
        rx = re.compile(r'(name="model" value="openrouter/)(' + find + r')(")')
        stale = {m.group(2) for m in rx.finditer(text) if m.group(2) != new_id}
        if not stale:
            continue
        text = rx.sub(lambda m: m.group(1) + new_id + m.group(3), text)
        for s in sorted(stale):
            changes.append(f"{fam}: {s} -> {new_id}")
    return text, changes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="patch the files (default: check-only)")
    args = ap.parse_args()

    try:
        ids = fetch_catalog()
    except Exception as e:
        print(f"[refresh-models] catalog fetch failed: {e}", file=sys.stderr)
        return 2
    latest = latest_per_family(ids)
    print(f"[refresh-models] resolved {len(latest)} families from {len(ids)} catalog models")

    all_changes = []
    # Only the picker's showcase chips (app.html) are auto-tracked. routes.py's
    # _OPENROUTER_SLUG map is curated by hand on a major bump.
    if APP_HTML.exists():
        original = APP_HTML.read_text()
        patched, changes = apply_to_text(original, latest)
        if changes:
            all_changes += changes
            if args.apply and patched != original:
                APP_HTML.write_text(patched)

    if not all_changes:
        print("[refresh-models] up to date — nothing to change.")
        return 0

    verb = "APPLIED" if args.apply else "AVAILABLE (run with --apply to patch)"
    print(f"[refresh-models] {len(all_changes)} chip update(s) {verb}:")
    for c in all_changes:
        print(f"  - {c}")
    print("[refresh-models] NOTE: chip display labels (e.g. 'MiniMax M3') are NOT "
          "auto-edited — eyeball them after an --apply bump.")
    # A machine-readable report next to the script, for the timer / notifier.
    (REPO / ".ark_model_refresh_report.json").write_text(
        json.dumps({"applied": args.apply, "changes": all_changes, "latest": latest}, indent=2)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
