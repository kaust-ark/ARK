#!/usr/bin/env bash
# ark_vm_reaper.sh - ARK Orchestrator VM Reaper (Phase 6)
#
# This script runs as a background daemon on the Orchestrator VM.
# It monitors:
#   1. The orchestrator process (via PID file)
#   2. The launcher heartbeat file (touched every ~60s by the webapp)
#
# If the orchestrator process has exited AND the launcher heartbeat is stale
# for more than HEARTBEAT_TIMEOUT_MINS minutes, the VM initiates shutdown
# to prevent runaway cost.
#
# Usage (started by run_orchestrator in OrchestratorCloudBackend):
#   nohup bash /home/<user>/<project>/ark_vm_reaper.sh <work_dir> <pid_file> > reaper.log 2>&1 &

set -euo pipefail

WORK_DIR="${1:?Usage: ark_vm_reaper.sh <work_dir> <pid_file>}"
PID_FILE="${2:?Usage: ark_vm_reaper.sh <work_dir> <pid_file>}"

HEARTBEAT_FILE="${WORK_DIR}/auto_research/state/launcher_heartbeat"
HEARTBEAT_TIMEOUT_MINS=30   # Shutdown if launcher silent for this long after orchestrator exits
POLL_INTERVAL_SECS=60

log() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] [REAPER] $*"
}

log "Reaper started. Work dir: ${WORK_DIR}, PID file: ${PID_FILE}"

while true; do
    sleep "${POLL_INTERVAL_SECS}"

    # --- Check if orchestrator is still alive ---
    ORCH_ALIVE=false
    if [[ -f "${PID_FILE}" ]]; then
        ORCH_PID=$(cat "${PID_FILE}" 2>/dev/null || echo "")
        if [[ -n "${ORCH_PID}" ]] && kill -0 "${ORCH_PID}" 2>/dev/null; then
            ORCH_ALIVE=true
        fi
    fi

    if "${ORCH_ALIVE}"; then
        # Orchestrator is running; nothing to do yet.
        continue
    fi

    log "Orchestrator process is no longer running."

    # --- Orchestrator is dead. Check launcher heartbeat. ---
    if [[ ! -f "${HEARTBEAT_FILE}" ]]; then
        log "No heartbeat file found. Waiting one more cycle before shutdown..."
        sleep "${POLL_INTERVAL_SECS}"
        # Recheck — if still no heartbeat, shutdown
        if [[ ! -f "${HEARTBEAT_FILE}" ]]; then
            log "Heartbeat file absent. Initiating VM shutdown."
            sudo poweroff
            exit 0
        fi
    fi

    # Get mtime of heartbeat in seconds since epoch
    HEARTBEAT_MTIME=$(stat -c '%Y' "${HEARTBEAT_FILE}" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    STALE_SECS=$(( NOW - HEARTBEAT_MTIME ))
    STALE_MINS=$(( STALE_SECS / 60 ))

    if (( STALE_MINS >= HEARTBEAT_TIMEOUT_MINS )); then
        log "Launcher heartbeat stale for ${STALE_MINS} min (> ${HEARTBEAT_TIMEOUT_MINS} min threshold)."
        log "Initiating VM shutdown to prevent orphaned cost."
        sudo poweroff
        exit 0
    else
        log "Launcher heartbeat is ${STALE_MINS} min old. Waiting for reconnect..."
    fi
done
