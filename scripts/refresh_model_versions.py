#!/usr/bin/env python3
"""Refresh the model-picker slugs from the live OpenRouter catalog.

The dashboard's model picker (website/dashboard/templates/app.html) and the
OpenRouter routing map (website/dashboard/routes.py `_OPENROUTER_SLUG`) hardcode
a slug per model family (e.g. ``openrouter/minimax/minimax-m3``). Those go stale
as vendors ship new versions. This script pulls https://openrouter.ai/api/v1/models,
finds the latest STABLE version per tracked family, and either reports the drift
(default, check-only) or patches the files in place (``--apply``).

"Stable" = the slug matches the family's exact ``base-<version>[suffix]`` shape
with the highest numeric version. Preview / experimental / dated / image / audio
/ ``-fast`` / ``-thinking`` variants are skipped — they share a family stem but
are not drop-in replacements, and surfacing one unattended could break a run.

Designed to be run by a systemd timer every 2 days. Default (no flag) is safe to
run unattended (it only reads + reports); ``--apply`` edits source — every change is
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


# Every picker in app.html: create (name="model"), continue (continue-model),
# restart (restart-model). All three are hand-duplicated, so a partial patch
# (the old bug) left continue/restart pointing at a stale slug while create
# advanced — a value/label mismatch and a wrong model on restart.
_PICKER_NAMES = ("model", "continue-model", "restart-model")


def _version_token(slug: str) -> str:
    """The trailing version substring of a slug's last segment, as it appears
    in the human label (kimi-k3 -> 'k3', claude-sonnet-5 -> '5',
    minimax-m3 -> 'm3', glm-5.2 -> '5.2', deepseek-v4-pro -> '4')."""
    seg = slug.rsplit("/", 1)[-1]
    m = re.search(r"(\d+(?:\.\d+)?)", seg)
    return m.group(1) if m else ""


def apply_to_text(text: str, latest: dict[str, str]) -> tuple[str, list[str]]:
    """Advance every showcase radio chip — value AND adjacent display label —
    across all three pickers. The value is matched anchored to the radio-chip
    context (``name="<picker>" value="openrouter/<id>"``) so dropdown
    ``<option>``s and native dash-format strings (claude-sonnet-4-6) are never
    touched. The label update replaces just the version token in the
    ``model-chip`` span that immediately follows the input, so 'Kimi K2.6' ->
    'Kimi K3' and 'Claude Sonnet 4.6' / 'Sonnet 4.6' -> '... 5' all work
    regardless of label phrasing."""
    changes = []
    for fam, find in SLUG_IN_FILE.items():
        new_id = latest.get(fam)
        if not new_id:
            continue
        names = "|".join(_PICKER_NAMES)
        # Capture the chip as a UNIT: input (with its slug) + the following
        # model-chip label text up to the nested model-meta span (or chip end).
        chip_rx = re.compile(
            r'(name="(?:' + names + r')" value="openrouter/)(' + find + r')(")'
            r'([\s\S]*?<span class="model-chip">)([^<]*)',
        )
        old_ver = None

        def _repl(m):
            nonlocal old_ver
            cur_slug = m.group(2)
            if cur_slug != new_id:
                old_ver = _version_token(cur_slug)
            label = m.group(5)
            new_ver = _version_token(new_id)
            ov = _version_token(cur_slug)
            if ov and new_ver and ov in label:
                label = label.replace(ov, new_ver)
            return m.group(1) + new_id + m.group(3) + m.group(4) + label

        new_text, n = chip_rx.subn(_repl, text)
        if n and old_ver is not None:
            text = new_text
            changes.append(f"{fam}: ...-{old_ver} -> {new_id} ({n} picker chip(s))")
    return text, changes


def _notify_admin(changes: list[str]) -> None:
    """Best-effort email to the primary admin when new models are available.

    Closes the automation loop: the timer runs check-only every 2 days, but a
    report file nobody reads is not a notification. On drift we tell the admin,
    who runs ``--apply`` + release (one command). Fully hands-off auto-apply is
    deliberately NOT wired: a bad slug would break every launch, so a human
    still gates the deploy — same observe-first stance as the delivery
    contract. Fail-silent: a notification hiccup must never fail the timer."""
    try:
        sys.path.insert(0, str(REPO))
        from website.dashboard.config import get_settings
        from website.dashboard.notify import send_failure_email
        s = get_settings()
        admins = getattr(s, "admin_emails", []) or []
        if not admins:
            return
        body = ("New OpenRouter models are available for the picker:\n\n  "
                + "\n  ".join(changes)
                + "\n\nApply + deploy:\n"
                  "  python scripts/refresh_model_versions.py --apply\n"
                  "  # review the diff, then: ark webapp release\n")
        send_failure_email(s, to_email=admins[0],
                           project_name="Model picker — updates available",
                           owner_email="model-refresh",
                           error=body, project_url="")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="patch the files in place (default: check-only, report drift)")
    ap.add_argument("--notify", action="store_true", help="email the admin when drift is found (for the timer)")
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
    print("[refresh-models] chip values AND display labels updated across all "
          "three pickers (create / continue / restart). Native direct-vendor "
          "chips (dash-format, e.g. claude-sonnet-4-6) are intentionally left alone.")
    # A machine-readable report next to the script, for the timer / notifier.
    (REPO / ".ark_model_refresh_report.json").write_text(
        json.dumps({"applied": args.apply, "changes": all_changes, "latest": latest}, indent=2)
    )
    # Close the loop: when the (check-only) timer finds drift, tell the admin.
    if args.notify and not args.apply:
        _notify_admin(all_changes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
