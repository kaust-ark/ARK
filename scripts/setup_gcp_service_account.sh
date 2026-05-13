#!/bin/bash
# =============================================================================
# ARK GCP Service Account Setup
#
# Creates a service account for ARK with the permissions needed to:
#   - Provision and tear down orchestrator and job VMs
#   - Boot instances from the ark-debian-base image family
#
# Usage: ./scripts/setup_gcp_service_account.sh [GCP_PROJECT] [SA_NAME]
#
# Outputs a key file to ark-gcp-key.json (add to .env as gcp_service_account_json)
# =============================================================================

set -e

PROJECT=${1:-$(gcloud config get-value project)}
SA_NAME=${2:-ark-orchestrator}
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
KEY_FILE="ark-gcp-key.json"

echo "Setting up ARK service account: $SA_EMAIL"
echo "Project: $PROJECT"

# 1. Create the service account
if gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT" &>/dev/null; then
    echo "Service account already exists, skipping creation."
else
    gcloud iam service-accounts create "$SA_NAME" \
        --project="$PROJECT" \
        --display-name="ARK Orchestrator"
fi

# 2. Bind required roles
ROLES=(
    "roles/compute.instanceAdmin.v1"   # create/delete/list instances
    "roles/compute.imageUser"          # boot from ark-debian-base image
    "roles/iam.serviceAccountUser"     # attach service account to spawned VMs
)

for ROLE in "${ROLES[@]}"; do
    echo "Binding $ROLE..."
    gcloud projects add-iam-policy-binding "$PROJECT" \
        --member="serviceAccount:$SA_EMAIL" \
        --role="$ROLE" \
        --quiet
done

# 3. Create and download a key
echo "Creating key file: $KEY_FILE"
gcloud iam service-accounts keys create "$KEY_FILE" \
    --iam-account="$SA_EMAIL" \
    --project="$PROJECT"

echo ""
echo "Done. Key written to $KEY_FILE"
echo "Add the contents to your config under orchestrator_compute_backend.gcp_service_account_json"
echo "Make sure $KEY_FILE is in .gitignore."
