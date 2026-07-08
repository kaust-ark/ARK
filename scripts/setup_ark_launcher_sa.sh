#!/usr/bin/env bash
#
# setup_ark_launcher_sa.sh — create + activate the central "ARK launcher" GCP
# service account that SkyPilot uses to provision compute.
#
# In the workspaces model (see SKYPILOT_PLAN.md), ONE central service account
# provisions into every user's project. Users grant this SA access to their own
# project via IAM (no key material ever leaves their cloud); a per-launch
# `active_workspace` just switches which project_id the launch targets.
#
# This script sets that SA up on the operator's own project and activates it
# locally so `sky launch` (and this webapp host's local SkyPilot API server)
# runs as the SA rather than a human's expiring user credentials.
#
# Idempotent: safe to re-run. It will NOT mint a second key if one already
# exists locally at $KEY_FILE.
#
# Usage:
#   scripts/setup_ark_launcher_sa.sh [PROJECT_ID]
#   PROJECT_ID defaults to the gcloud active project.

set -euo pipefail

PROJECT_ID="${1:-$(gcloud config get-value project 2>/dev/null)}"
SA_ID="ark-launcher"
SA_DISPLAY="ARK SkyPilot Launcher"
SA_EMAIL="${SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"

# Key + activation artifacts live OUTSIDE the repo so they can never be
# committed. 0600, in the user's config dir.
KEY_DIR="${HOME}/.config/ark"
KEY_FILE="${KEY_DIR}/ark-launcher-sa-key.json"
ENV_FILE="${KEY_DIR}/launcher-sa.env"

# "Medium permissions" set from SkyPilot's GCP admin docs — enough for SkyPilot
# to provision/teardown VMs, use buckets, and (via securityAdmin, needed ONCE)
# create its own skypilot-v1 SA. securityAdmin can be downgraded to roleViewer
# after the first successful `sky launch --infra gcp`.
ROLES=(
  roles/browser
  roles/compute.admin
  roles/iam.serviceAccountAdmin
  roles/iam.serviceAccountUser
  roles/serviceusage.serviceUsageAdmin
  roles/storage.admin
  roles/iam.securityAdmin
)

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN\033[0m %s\n' "$*" >&2; }
# Portable sleep (the sandbox blocks the bare `sleep` builtin).
nap() { python3 -c "import time,sys; time.sleep(float(sys.argv[1]))" "$1"; }

# Retry a command through GCP's eventual consistency (a freshly-created SA is
# not immediately visible to the IAM policy service).
retry() {
  local n=0 max=8
  until "$@"; do
    n=$((n + 1))
    if [[ $n -ge $max ]]; then
      warn "Command failed after ${max} attempts: $*"
      return 1
    fi
    warn "attempt ${n}/${max} failed; waiting for propagation..."
    nap 6
  done
}

if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  echo "ERROR: no project id (pass as \$1 or set 'gcloud config set project ...')" >&2
  exit 1
fi

log "Project: ${PROJECT_ID}"
log "Service account: ${SA_EMAIL}"

# 1. Create the SA (idempotent).
if gcloud iam service-accounts describe "${SA_EMAIL}" \
     --project "${PROJECT_ID}" >/dev/null 2>&1; then
  log "Service account already exists — skipping create."
else
  log "Creating service account '${SA_ID}'..."
  gcloud iam service-accounts create "${SA_ID}" \
    --project "${PROJECT_ID}" \
    --display-name "${SA_DISPLAY}"
fi

# 2. Grant the medium-permission roles on THIS project (add-iam-policy-binding
#    is idempotent). This makes the SA able to launch into the operator's own
#    project — used for testing steps 1-3. User projects are granted separately,
#    by each user, on their own project.
for role in "${ROLES[@]}"; do
  log "Granting ${role}..."
  retry gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member "serviceAccount:${SA_EMAIL}" \
    --role "${role}" \
    --condition=None \
    --quiet >/dev/null
done

# 3. Create a key (only if we don't already hold one locally — re-running must
#    not accumulate orphaned keys on the SA).
mkdir -p "${KEY_DIR}"
chmod 700 "${KEY_DIR}"
if [[ -f "${KEY_FILE}" ]]; then
  log "Key already present at ${KEY_FILE} — skipping key creation."
else
  log "Creating key -> ${KEY_FILE}"
  if ! gcloud iam service-accounts keys create "${KEY_FILE}" \
        --iam-account "${SA_EMAIL}" \
        --project "${PROJECT_ID}" 2>keyerr.tmp; then
    warn "Key creation failed:"
    cat keyerr.tmp >&2
    rm -f keyerr.tmp
    warn "Your org may block SA key creation (org policy"
    warn "'iam.disableServiceAccountKeyCreation'). If so, use SA impersonation"
    warn "instead: gcloud config set auth/impersonate_service_account ${SA_EMAIL}"
    exit 1
  fi
  rm -f keyerr.tmp
  chmod 600 "${KEY_FILE}"
fi

# 4. Activate the SA for gcloud AND application-default credentials (SkyPilot
#    GCP auth reads both — the activated gcloud account for project/config and
#    GOOGLE_APPLICATION_CREDENTIALS for ADC).
log "Activating service account for gcloud (new keys take ~30-60s to sign)..."
retry gcloud auth activate-service-account "${SA_EMAIL}" \
  --key-file "${KEY_FILE}" \
  --project "${PROJECT_ID}"
gcloud config set project "${PROJECT_ID}" >/dev/null

# 5. Persist the ADC env var to a sourceable file (a script can't export into
#    the parent shell). Source this from the webapp host's environment.
cat > "${ENV_FILE}" <<EOF
# Source me so SkyPilot/gcloud use the ARK launcher SA.
export GOOGLE_APPLICATION_CREDENTIALS="${KEY_FILE}"
export CLOUDSDK_CORE_PROJECT="${PROJECT_ID}"
EOF
chmod 600 "${ENV_FILE}"

log "Done."
echo
echo "  SA:        ${SA_EMAIL}"
echo "  Key:       ${KEY_FILE}"
echo "  Env file:  ${ENV_FILE}"
echo
echo "  To use in this shell / the webapp host, run:"
echo "      source ${ENV_FILE}"
echo
echo "  gcloud is now ACTIVE as the SA. To switch back to your user account:"
echo "      gcloud config set account <your-user>@kaust.edu.sa"
