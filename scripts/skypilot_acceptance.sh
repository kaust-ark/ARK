#!/usr/bin/env bash
#
# PR5 acceptance + parity driver (folded Phases 5+6, ADR-0010, SKYPILOT_PLAN §5).
#
# Automates the whole PR5 gate end-to-end:
#   1. PREFLIGHT   — sky installed? which clouds/K8s can it reach?
#   2. PARITY      — offline invariant: mocked-skypilot + slurm/local suites green
#                    (the half that needs no clouds; safe to run anywhere).
#   3. ACCEPTANCE  — real provision→reachable→teardown across the chosen clouds
#                    (the @pytest.mark.skypilot suite; costs money).
#   4. ORPHAN SWEEP— assert no `ark-*` clusters survive the run; offer to reap.
#   5. SUMMARY     — check results against the SKYPILOT_PLAN §5 acceptance bullets.
#
# You supply only credentials + which clouds. Everything else is automatic.
#
# Usage:
#   scripts/skypilot_acceptance.sh                 # auto-detect clouds from `sky check`
#   scripts/skypilot_acceptance.sh --clouds aws,gcp,kubernetes
#   scripts/skypilot_acceptance.sh --offline-only  # parity half only (no clouds/$$)
#   scripts/skypilot_acceptance.sh --no-sweep      # skip the post-run orphan reap prompt
#   scripts/skypilot_acceptance.sh --yes           # non-interactive: auto-reap orphans
#
# Env:
#   ARK_PY        python to use (default: .venv312/bin/python if present, else python3)
#   ARK_SKYPILOT_ACCEPTANCE_INSTANCE_<CLOUD> / _REGION_<CLOUD> / _SPOT=1  (see the test)
#
set -euo pipefail

# ── locate repo + python ─────────────────────────────────────────────────────
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
if [[ -n "${ARK_PY:-}" ]]; then PY="$ARK_PY"
elif [[ -x .venv312/bin/python ]]; then PY=".venv312/bin/python"
else PY="python3"; fi

# ── args ─────────────────────────────────────────────────────────────────────
CLOUDS=""
OFFLINE_ONLY=0
DO_SWEEP=1
ASSUME_YES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --clouds) CLOUDS="$2"; shift 2 ;;
    --clouds=*) CLOUDS="${1#*=}"; shift ;;
    --offline-only) OFFLINE_ONLY=1; shift ;;
    --no-sweep) DO_SWEEP=0; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
red() { printf '\033[31m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
rule() { printf '%s\n' "────────────────────────────────────────────────────────"; }

PARITY_OK=0
ACCEPT_OK=0
ACCEPT_RAN=0
SWEEP_OK=0

# ── 1. PREFLIGHT ─────────────────────────────────────────────────────────────
rule; bold "1. PREFLIGHT"; rule
bold "python: $PY ($($PY --version 2>&1))"
$PY -c "import ark" 2>/dev/null && green "  ark importable" || { red "  ark NOT importable — create a venv and 'pip install -e .[research]'"; exit 1; }

if [[ $OFFLINE_ONLY -eq 0 ]]; then
  if ! command -v sky >/dev/null 2>&1; then
    red "  sky CLI not found. Install: pip install 'ark[skypilot]' && pip install 'skypilot[gcp,aws,kubernetes]'"
    yellow "  (run with --offline-only to do just the parity half without clouds.)"
    exit 1
  fi
  green "  sky: $(sky --version 2>/dev/null | head -1)"
  bold "  sky check (enabled clouds):"
  sky check 2>&1 | sed 's/^/    /' || true

  # Auto-detect enabled clouds from `sky check` if none given. We look for the
  # canonical "<Cloud>: enabled" lines SkyPilot prints.
  if [[ -z "$CLOUDS" ]]; then
    DETECTED="$(sky check 2>/dev/null \
      | grep -iE '^\s*(AWS|GCP|Azure|Kubernetes)\b' \
      | grep -iE 'enabled' \
      | sed -E 's/[^A-Za-z].*$//' | tr 'A-Z' 'a-z' | paste -sd, -)" || true
    CLOUDS="$DETECTED"
    [[ -n "$CLOUDS" ]] && yellow "  auto-detected enabled clouds: $CLOUDS"
  fi
fi

# ── 2. PARITY (offline invariant — always runs) ──────────────────────────────
rule; bold "2. PARITY (offline — SLURM/local + mocked-skypilot must stay green)"; rule
# The hard invariant (SKYPILOT_PLAN §5): slurm/local suites still pass, and the
# skypilot mocked suites (PR1–4 regression) stay green. All CI-safe (unmarked).
PARITY_SUITES=(
  tests/unit/test_skypilot_seam.py
  tests/unit/test_skypilot_backend.py
  tests/unit/test_skypilot_launcher.py
  tests/unit/test_config_matrix.py
  tests/unit/test_compute.py
  tests/unit/test_launch_dispatch.py
  tests/unit/test_launcher.py
)
if $PY -m pytest "${PARITY_SUITES[@]}" -q -m "not skypilot and not network and not gcp"; then
  PARITY_OK=1; green "  PARITY: green"
else
  red "  PARITY: FAILED — the offline invariant is broken; stop and fix before acceptance."
fi

# ── 3. ACCEPTANCE (real clouds) ──────────────────────────────────────────────
if [[ $OFFLINE_ONLY -eq 1 ]]; then
  yellow "  --offline-only: skipping real-cloud acceptance."
elif [[ -z "$CLOUDS" ]]; then
  rule; bold "3. ACCEPTANCE"; rule
  yellow "  No clouds enabled/selected — nothing to provision."
  yellow "  Enable clouds (aws configure / gcloud auth / kubectl config) then re-run,"
  yellow "  or pass --clouds aws,gcp,kubernetes explicitly."
else
  rule; bold "3. ACCEPTANCE (real provisioning across: $CLOUDS)"; rule
  yellow "  This provisions real VMs/pods and COSTS MONEY. Each cloud leg tears itself"
  yellow "  down; the orphan sweep in step 4 double-checks."
  ACCEPT_RAN=1
  if ARK_SKYPILOT_ACCEPTANCE_CLOUDS="$CLOUDS" \
       $PY -m pytest tests/integration/test_skypilot_acceptance.py -m skypilot -s -v; then
    ACCEPT_OK=1; green "  ACCEPTANCE: green across $CLOUDS"
  else
    red "  ACCEPTANCE: FAILED — inspect output above; run the sweep to reap any leftovers."
  fi
fi

# ── 4. ORPHAN SWEEP ──────────────────────────────────────────────────────────
if [[ $OFFLINE_ONLY -eq 0 && $DO_SWEEP -eq 1 ]] && command -v sky >/dev/null 2>&1; then
  rule; bold "4. ORPHAN SWEEP (no ark-* cluster may survive)"; rule
  # --refresh forces a real cloud query rather than trusting cached state.
  ORPHANS="$(sky status --refresh 2>/dev/null | grep -iE '^\s*ark-' | awk '{print $1}' || true)"
  if [[ -z "$ORPHANS" ]]; then
    SWEEP_OK=1; green "  no orphaned ark-* clusters — teardown verified."
  else
    red "  ORPHANS DETECTED:"; echo "$ORPHANS" | sed 's/^/    /'
    if [[ $ASSUME_YES -eq 1 ]]; then REPLY="y";
    else read -r -p "  sky down these now? [y/N] " REPLY || REPLY="n"; fi
    if [[ "$REPLY" =~ ^[Yy]$ ]]; then
      # shellcheck disable=SC2086
      sky down -y $ORPHANS && green "  reaped." || red "  reap failed — check the cloud console manually."
      # Re-verify.
      REMAIN="$(sky status --refresh 2>/dev/null | grep -iE '^\s*ark-' | awk '{print $1}' || true)"
      [[ -z "$REMAIN" ]] && SWEEP_OK=1 || red "  still present: $REMAIN"
    else
      yellow "  left in place — REAP MANUALLY (they bill by the hour): sky down $ORPHANS"
    fi
  fi
fi

# ── 5. SUMMARY (SKYPILOT_PLAN §5) ────────────────────────────────────────────
rule; bold "5. SUMMARY vs SKYPILOT_PLAN §5 acceptance"; rule
mark() {
  case "$1" in
    1) green "  [PASS] $2" ;;
    -) yellow "  [SKIP] $2" ;;
    *) red "  [FAIL] $2" ;;
  esac
}
mark "$PARITY_OK" "slurm/local + mocked-skypilot suites green (hard invariant)"
if [[ $OFFLINE_ONLY -eq 1 || $ACCEPT_RAN -eq 0 ]]; then
  mark "-" "real run across >=2 clouds + BYO-K8s (not run this pass)"
  mark "-" "teardown / no orphaned resources (not run this pass)"
else
  mark "$ACCEPT_OK" "real run across clouds: $CLOUDS (provision + reachable)"
  mark "$SWEEP_OK" "teardown verified / no orphaned resources"
fi
echo
if [[ $PARITY_OK -eq 1 && $ACCEPT_OK -eq 1 && $SWEEP_OK -eq 1 && $ACCEPT_RAN -eq 1 ]]; then
  green "ALL PR5 GATES PASSED across ≥1 cloud. Repeat with ≥2 clouds + a BYO-K8s"
  green "context to satisfy §5 fully, then flip ADR-0010 → Accepted."
  exit 0
elif [[ $OFFLINE_ONLY -eq 1 && $PARITY_OK -eq 1 ]]; then
  green "Offline parity green. Re-run without --offline-only on a creds machine for the cloud gate."
  exit 0
else
  yellow "PR5 not fully satisfied yet — see the [FAIL]/[SKIP] lines above."
  exit 1
fi
