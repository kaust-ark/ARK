#!/usr/bin/env bash
# check_gcp_image_env.sh - Verify conda env + ARK readiness on a GCP image
#
# Spins up a temporary VM using the same image/zone/config as the orchestrator,
# runs a battery of checks, prints a report, then tears the VM down.
#
# Usage:
#   bash scripts/check_gcp_image_env.sh [OPTIONS]
#
# Options:
#   --project    GCP project ID           (default: kaust-pf2023-marco)
#   --zone       GCP zone                 (default: us-central1-a)
#   --image      Image family             (default: ark-debian-base)
#   --machine    Machine type             (default: n4-standard-2)
#   --conda-env  Conda env to check       (default: ark-base)
#   --ssh-key    Path to SSH private key  (default: ~/.ssh/id_rsa)
#   --ssh-user   SSH username             (default: ubuntu)
#   --sa-json    Path to GCP service account JSON (optional)
#   --keep       Don't delete the VM after checks (for manual inspection)

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
GCP_PROJECT="kaust-pf2023-marco"
ZONE="us-central1-a"
IMAGE_FAMILY="ark-debian-base"
MACHINE_TYPE="n4-standard-2"
CONDA_ENV="ark-base"
SSH_KEY="${HOME}/.ssh/id_rsa"
SSH_USER="ubuntu"
SA_JSON=""
NETWORK=""
KEEP_VM=false

# ── Arg parsing ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --project)   GCP_PROJECT="$2";  shift 2 ;;
        --zone)      ZONE="$2";         shift 2 ;;
        --image)     IMAGE_FAMILY="$2"; shift 2 ;;
        --machine)   MACHINE_TYPE="$2"; shift 2 ;;
        --conda-env) CONDA_ENV="$2";    shift 2 ;;
        --ssh-key)   SSH_KEY="$2";      shift 2 ;;
        --ssh-user)  SSH_USER="$2";     shift 2 ;;
        --sa-json)   SA_JSON="$2";      shift 2 ;;
        --network)   NETWORK="$2";      shift 2 ;;
        --keep)      KEEP_VM=true;      shift   ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────
INSTANCE_NAME="ark-check-$(date +%s | tail -c 5)"
INSTANCE_IP=""
SA_TMP=""

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
ok()   { echo "  [PASS] $*"; }
fail() { echo "  [FAIL] $*"; }
info() { echo "  [INFO] $*"; }

gcloud_cmd() {
    if [[ -n "${SA_TMP}" ]]; then
        CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE="${SA_TMP}" gcloud "$@"
    else
        gcloud "$@"
    fi
}

ssh_exec() {
    ssh -i "${SSH_KEY}" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR \
        -o ConnectTimeout=10 \
        "${SSH_USER}@${INSTANCE_IP}" "$@"
}

cleanup() {
    if [[ "${KEEP_VM}" == "true" ]]; then
        log "Skipping teardown (--keep). Instance: ${INSTANCE_NAME} (${INSTANCE_IP})"
        log "To delete manually: gcloud compute instances delete ${INSTANCE_NAME} --zone=${ZONE} --project=${GCP_PROJECT} --quiet"
    elif [[ -n "${INSTANCE_NAME}" ]]; then
        log "Deleting VM ${INSTANCE_NAME}..."
        gcloud_cmd compute instances delete "${INSTANCE_NAME}" \
            --zone="${ZONE}" --project="${GCP_PROJECT}" --quiet 2>/dev/null || true
        log "VM deleted."
    fi
    if [[ -n "${SA_TMP}" ]]; then
        rm -f "${SA_TMP}"
    fi
}
trap cleanup EXIT

# ── Setup service account credentials if provided ─────────────────────────────
if [[ -n "${SA_JSON}" && -f "${SA_JSON}" ]]; then
    SA_TMP="$(mktemp /tmp/ark_sa_XXXXXX.json)"
    cp "${SA_JSON}" "${SA_TMP}"
    log "Using service account credentials from ${SA_JSON}"
fi

# ── Read SSH public key ────────────────────────────────────────────────────────
PUB_KEY_PATH="${SSH_KEY}.pub"
if [[ -f "${PUB_KEY_PATH}" ]]; then
    PUB_KEY_CONTENT="$(cat "${PUB_KEY_PATH}")"
else
    PUB_KEY_CONTENT="$(ssh-keygen -y -f "${SSH_KEY}" 2>/dev/null || true)"
fi

if [[ -z "${PUB_KEY_CONTENT}" ]]; then
    echo "ERROR: Could not read public key from ${SSH_KEY}. Provide a valid SSH key."
    exit 1
fi

# ── Provision VM ──────────────────────────────────────────────────────────────
log "Provisioning check VM: ${INSTANCE_NAME}"
log "  Image:   ${IMAGE_FAMILY} (project: ${GCP_PROJECT})"
log "  Zone:    ${ZONE}"
log "  Machine: ${MACHINE_TYPE}"

CREATE_STDERR=$(mktemp)
gcloud_cmd compute instances create "${INSTANCE_NAME}" \
    --zone="${ZONE}" \
    --project="${GCP_PROJECT}" \
    --machine-type="${MACHINE_TYPE}" \
    --image-family="${IMAGE_FAMILY}" \
    --image-project="${GCP_PROJECT}" \
    --metadata="ssh-keys=${SSH_USER}:${PUB_KEY_CONTENT}" \
    --scopes=cloud-platform \
    ${NETWORK:+--network="${NETWORK}"} \
    --format=json > /dev/null 2>"${CREATE_STDERR}" || {
        echo "ERROR: Failed to create VM:"
        cat "${CREATE_STDERR}"
        rm -f "${CREATE_STDERR}"
        exit 1
    }
rm -f "${CREATE_STDERR}"

# Use describe to get the IP reliably (avoids parsing mixed gcloud output)
log "Fetching instance IP..."
for i in $(seq 1 10); do
    INSTANCE_IP=$(gcloud_cmd compute instances describe "${INSTANCE_NAME}" \
        --zone="${ZONE}" --project="${GCP_PROJECT}" \
        --format="get(networkInterfaces[0].accessConfigs[0].natIP)" 2>/dev/null || true)
    if [[ -n "${INSTANCE_IP}" && "${INSTANCE_IP}" != "None" ]]; then
        break
    fi
    sleep 5
done

if [[ -z "${INSTANCE_IP}" || "${INSTANCE_IP}" == "None" ]]; then
    echo "ERROR: Could not get external IP. The VPC may not assign public IPs."
    exit 1
fi

log "VM created: ${INSTANCE_NAME} (${INSTANCE_IP})"

# ── Wait for SSH ──────────────────────────────────────────────────────────────
log "Waiting for SSH..."
for i in $(seq 1 30); do
    if ssh_exec "echo ready" &>/dev/null; then
        log "SSH ready (attempt ${i})"
        break
    fi
    if [[ $i -eq 30 ]]; then
        echo "ERROR: SSH timed out after 300s."
        exit 1
    fi
    sleep 10
done

# ── Run checks ────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
echo "  ARK GCP Image Readiness Check"
echo "  Instance : ${INSTANCE_NAME} (${INSTANCE_IP})"
echo "  Image    : ${IMAGE_FAMILY}"
echo "  Conda env: ${CONDA_ENV}"
echo "════════════════════════════════════════════════════════════"
echo ""

PASS=0
FAIL=0

run_check() {
    local label="$1" cmd="$2" output
    # Base64-encode and pipe to bash to avoid all SSH quoting/arg-splitting issues.
    # Passing multiple args to ssh_exec causes SSH to join them with spaces, breaking
    # "bash -lc <cmd>" into bash seeing only the first token as the -c script.
    local b64
    b64=$(printf '%s' "$cmd" | base64 | tr -d '\n')
    if output=$(ssh_exec "echo $b64 | base64 -d | bash -l" 2>&1); then
        ok "${label}"
        echo "     → ${output}" | head -3
        PASS=$(( PASS + 1 ))
    else
        fail "${label}"
        echo "     → ${output}" | head -5
        FAIL=$(( FAIL + 1 ))
    fi
}

# 1. OS info
echo "── System ───────────────────────────────────────────────────"
run_check "OS release" "cat /etc/os-release | grep PRETTY_NAME"
run_check "Disk space" "df -h / | tail -1"
run_check "RAM" "free -h | grep Mem"

# 2. Conda
echo ""
echo "── Conda ────────────────────────────────────────────────────"
CONDA_PATH_OUTPUT=$(ssh_exec "bash -lc 'which conda || find /opt /home /root -name conda -type f 2>/dev/null | head -3'" 2>&1 || true)
if [[ -n "${CONDA_PATH_OUTPUT}" ]]; then
    ok "conda found: ${CONDA_PATH_OUTPUT}"
    PASS=$(( PASS + 1 ))
else
    fail "conda not found on PATH or common locations"
    FAIL=$(( FAIL + 1 ))
fi

run_check "conda version" "conda --version"
run_check "conda envs list" "conda env list"

# 3. Target conda env
echo ""
echo "── Conda env: ${CONDA_ENV} ───────────────────────────────────"
ENV_EXISTS=$(ssh_exec "bash -lc 'conda env list | grep -w ${CONDA_ENV}'" 2>&1 || true)
if [[ -n "${ENV_EXISTS}" ]]; then
    ok "conda env '${CONDA_ENV}' exists"
    info "${ENV_EXISTS}"
    PASS=$(( PASS + 1 ))

    # Python version in env
    run_check "Python version in ${CONDA_ENV}" \
        "conda run -n ${CONDA_ENV} python --version"

    # Check ark package
    ARK_CMD="conda run -n ${CONDA_ENV} python -c 'import ark; print(ark.__file__)' 2>&1"
    ARK_B64=$(printf '%s' "$ARK_CMD" | base64 | tr -d '\n')
    ARK_CHECK=$(ssh_exec "echo $ARK_B64 | base64 -d | bash -l" 2>&1 || true)
    if echo "${ARK_CHECK}" | grep -qE "/__init__\.py"; then
        ok "ark package importable in ${CONDA_ENV}"
        info "${ARK_CHECK}"
        PASS=$(( PASS + 1 ))
    else
        fail "ark package NOT importable in ${CONDA_ENV}"
        info "${ARK_CHECK}"
        FAIL=$(( FAIL + 1 ))
    fi

    # Check ark.cli
    CLI_CMD="conda run -n ${CONDA_ENV} python -m ark.cli --help 2>&1 | head -3"
    CLI_B64=$(printf '%s' "$CLI_CMD" | base64 | tr -d '\n')
    CLI_CHECK=$(ssh_exec "echo $CLI_B64 | base64 -d | bash -l" 2>&1 || true)
    if echo "${CLI_CHECK}" | grep -qiE "usage|Commands|Options"; then
        ok "ark.cli invocable"
        PASS=$(( PASS + 1 ))
    else
        fail "ark.cli failed to run"
        info "${CLI_CHECK}"
        FAIL=$(( FAIL + 1 ))
    fi

    # Key dependencies
    echo ""
    echo "── Key dependencies in ${CONDA_ENV} ─────────────────────────"
    for pkg in anthropic google-genai openai pyyaml sqlmodel; do
        # Map package install name → Python import name
        case "$pkg" in
            pyyaml)       import_name="yaml" ;;
            google-genai) import_name="google.genai" ;;
            *)            import_name="${pkg//-/_}" ;;
        esac
        DEP_CMD="conda run -n ${CONDA_ENV} python -c 'import ${import_name}; print(getattr(${import_name}, \"__version__\", \"ok\"))' 2>&1"
        DEP_B64=$(printf '%s' "$DEP_CMD" | base64 | tr -d '\n')
        DEP_CHECK=$(ssh_exec "echo $DEP_B64 | base64 -d | bash -l" 2>&1 || true)
        # Fail if output contains any error indicator (use grep without -v so it's line-agnostic)
        if ! echo "${DEP_CHECK}" | grep -qE "Error|No module|Traceback|failed"; then
            ok "${pkg}: ${DEP_CHECK}"
            PASS=$(( PASS + 1 ))
        else
            fail "${pkg}: NOT found"
            info "${DEP_CHECK}"
            FAIL=$(( FAIL + 1 ))
        fi
    done
else
    fail "conda env '${CONDA_ENV}' does NOT exist"
    info "Available envs:"
    ssh_exec "bash -lc 'conda env list'" 2>&1 | sed 's/^/     /' || true
    FAIL=$(( FAIL + 1 ))
fi

# 4. System tools
echo ""
echo "── System tools ─────────────────────────────────────────────"
for tool in rsync gcloud sudo; do
    TOOL_PATH=$(ssh_exec "bash -lc 'which ${tool}'" 2>&1 || true)
    if [[ -n "${TOOL_PATH}" ]]; then
        ok "${tool}: ${TOOL_PATH}"
        PASS=$(( PASS + 1 ))
    else
        fail "${tool}: not found"
        FAIL=$(( FAIL + 1 ))
    fi
done

# sudo poweroff (reaper needs this without password)
SUDO_CHECK=$(ssh_exec "bash -lc 'sudo -n poweroff --help 2>&1 || sudo -n true 2>&1'" 2>&1 || true)
if echo "${SUDO_CHECK}" | grep -qv "password\|sudo:"; then
    ok "sudo passwordless: yes"
    PASS=$(( PASS + 1 ))
else
    fail "sudo passwordless: NO (reaper shutdown will fail)"
    info "${SUDO_CHECK}"
    FAIL=$(( FAIL + 1 ))
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Results: ${PASS} passed, ${FAIL} failed"
if [[ ${FAIL} -eq 0 ]]; then
    echo "  Status : READY - image looks good for ARK orchestration"
else
    echo "  Status : NOT READY - fix the failing checks before retrying"
fi
echo "════════════════════════════════════════════════════════════"
echo ""

# Exit with failure code if any checks failed
[[ ${FAIL} -eq 0 ]]
