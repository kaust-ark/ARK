#!/bin/bash
# =============================================================================
# ARK GCP Image Builder
#
# Provisions a temporary VM, runs setup_ark_host.sh, and saves a Machine Image.
# Usage: ./scripts/build_ark_gcp_image.sh [GCP_PROJECT] [ZONE]
# =============================================================================

set -e

PROJECT=${1:-$(gcloud config get-value project)}
ZONE=${2:-us-central1-a}
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
INSTANCE_NAME="ark-image-builder-$TIMESTAMP"
IMAGE_NAME="ark-job-v1-$TIMESTAMP"

echo "Building ARK GCP Image: $IMAGE_NAME"
echo "Project: $PROJECT, Zone: $ZONE"

# 1. Create temporary instance
gcloud compute instances create "$INSTANCE_NAME" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --machine-type="n2-standard-4" \
    --image-family="debian-12" \
    --image-project="debian-cloud" \
    --boot-disk-size="50GB" \
    --metadata="serial-port-enable=1"

# Wait for SSH to be ready
echo "Waiting for SSH..."
sleep 30

# 2. Push setup scripts and environment.yml
gcloud compute scp scripts/setup_ark_host.sh environment.yml "$INSTANCE_NAME":~/ --zone="$ZONE" --project="$PROJECT"

# 3. Run setup
echo "Running setup script on VM..."
gcloud compute ssh "$INSTANCE_NAME" --zone="$ZONE" --project="$PROJECT" --command="bash ~/setup_ark_host.sh"

# 4. Stop instance (required for clean imaging)
echo "Stopping instance..."
gcloud compute instances stop "$INSTANCE_NAME" --zone="$ZONE" --project="$PROJECT"

# 5. Create image
echo "Creating image $IMAGE_NAME..."
gcloud compute images create "$IMAGE_NAME" \
    --project="$PROJECT" \
    --source-disk="$INSTANCE_NAME" \
    --source-disk-zone="$ZONE" \
    --family="ark-job"

# 6. Cleanup
echo "Cleaning up temporary instance..."
gcloud compute instances delete "$INSTANCE_NAME" --zone="$ZONE" --project="$PROJECT" --quiet

echo "Successfully built ARK GCP Image: $IMAGE_NAME"
