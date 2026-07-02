#!/usr/bin/env bash
# =============================================================================
# ARK — container end-to-end boundary test.
#
# Stands up the real webapp (control plane) and a fully isolated orchestrator
# container that talks to it ONLY over the /v1 HTTP boundary (no shared DB/FS),
# with the LLM mocked via a fake `openhands`. Proves the thing the in-process
# tests can't: a real orchestrator process driving the boundary across the wire.
#
# Usage:
#   scripts/e2e_boundary.sh                # E2E_MODE=loop (default): 1 iteration
#   E2E_MODE=apply scripts/e2e_boundary.sh # faster: one read-only agent, no loop
#   KEEP=1 scripts/e2e_boundary.sh         # leave containers up for inspection
#   NOBUILD=1 scripts/e2e_boundary.sh      # skip image build (reuse existing)
#
# Requires: docker + docker compose v2. First run builds two heavy images.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
COMPOSE=(docker compose -f "$ROOT/docker/docker-compose.e2e.yml" -p ark-e2e)

E2E_MODE="${E2E_MODE:-loop}"
MODEL="${E2E_MODEL:-anthropic/claude-haiku-4-5}"

say()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

cleanup() {
  local code=$?
  if [[ "${KEEP:-0}" == "1" ]]; then
    say "KEEP=1 — leaving containers up. Tear down with:"
    echo "  ${COMPOSE[*]} down -v"
  else
    say "Tearing down"
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  exit $code
}
trap cleanup EXIT

[[ "$E2E_MODE" == "loop" || "$E2E_MODE" == "apply" ]] || die "E2E_MODE must be 'loop' or 'apply'"

# ── 1. Build images (heavy on first run) ─────────────────────────────────────
if [[ "${NOBUILD:-0}" != "1" ]]; then
  say "Building webapp + job-e2e images (first run is slow)"
  # job-e2e is FROM ark-job:latest, so that base must exist first.
  docker image inspect ark-job:latest >/dev/null 2>&1 || {
    say "Building base ark-job image (required by ark-job-e2e)"
    docker build --platform linux/amd64 -f "$ROOT/docker/Dockerfile.job" -t ark-job:latest "$ROOT"
  }
  # --profile job so the profiled `job` service is included in the build (a bare
  # `compose build` silently skips services behind an inactive profile).
  "${COMPOSE[@]}" --profile job build
fi

# ── 2. Start the control plane ───────────────────────────────────────────────
say "Starting webapp (control plane)"
"${COMPOSE[@]}" up -d webapp

say "Waiting for webapp health"
for i in $(seq 1 60); do
  if curl -fs "http://localhost:9527/dashboard/health" >/dev/null 2>&1; then
    echo "  healthy"; break
  fi
  [[ $i == 60 ]] && { "${COMPOSE[@]}" logs webapp | tail -40; die "webapp did not become healthy"; }
  sleep 2
done

# ── 3. Seed a project + mint a scoped job token (inside the webapp container) ──
say "Seeding project + minting job token"
SEED_OUT="$("${COMPOSE[@]}" exec -T webapp python /e2e/seed_project.py)"
PROJECT_ID="$(printf '%s\n' "$SEED_OUT" | sed -n 's/^PROJECT_ID=//p')"
JOB_TOKEN="$(printf '%s\n' "$SEED_OUT" | sed -n 's/^JOB_TOKEN=//p')"
[[ -n "$PROJECT_ID" && -n "$JOB_TOKEN" ]] || die "seeding failed (no PROJECT_ID/JOB_TOKEN)"
echo "  project=$PROJECT_ID"

# ── 4. Run the real orchestrator over the boundary (isolated container) ───────
COMMON_ARGS=(--project e2e --project-id "$PROJECT_ID"
             --project-dir /app/e2e-project --model "$MODEL" --no-research)
if [[ "$E2E_MODE" == "apply" ]]; then
  MODE_ARGS=(--apply-instruction "Summarize the paper idea in one sentence."
             --apply-scope answer)
  EXPECT_ACK=()
else
  MODE_ARGS=(--iterations 1 --max-days 1)
  EXPECT_ACK=(--expect-command-ack)
fi

say "Running orchestrator (mode=$E2E_MODE) over /v1 — no shared volume"
set +e
"${COMPOSE[@]}" run --rm -e ARK_CONTROL_PLANE_TOKEN="$JOB_TOKEN" \
  job "${COMMON_ARGS[@]}" "${MODE_ARGS[@]}"
JOB_RC=$?
set -e
echo "  job exited rc=$JOB_RC"

# ── 5. Assert the boundary from the control plane's side ─────────────────────
say "Asserting boundary crossings landed"
# Note the ${arr[@]+"${arr[@]}"} idiom: expanding an empty array as "${arr[@]}"
# under `set -u` is an "unbound variable" error on bash 3.2 (macOS default).
"${COMPOSE[@]}" exec -T webapp python /e2e/assert_boundary.py "$PROJECT_ID" \
  ${EXPECT_ACK[@]+"${EXPECT_ACK[@]}"} \
  || die "boundary assertions FAILED"

say "E2E boundary test PASSED (mode=$E2E_MODE)"
