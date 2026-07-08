#!/bin/bash
# =============================================================================
# ARK GCP Image Builder
#
# Provisions a temporary VM, runs setup_ark_host.sh, and saves a Machine Image.
# Usage: ./scripts/build_ark_gcp_image.sh [GCP_PROJECT] [ZONE] [VERSION]
#
# Env vars (for projects without a `default` VPC — e.g. the central provider
# project, which uses a custom-mode VPC): NETWORK=<vpc-name> SUBNET=<subnet-name>.
# A custom-mode VPC has no auto subnet, so --subnet is required alongside
# --network; pick a subnet in the same region as ZONE.
# =============================================================================

set -e

PROJECT=${1:-$(gcloud config get-value project)}
ZONE=${2:-us-central1-a}
VERSION=${3:-v1}
INSTANCE_NAME="ark-image-builder-$(date +%Y%m%d-%H%M%S)"
IMAGE_NAME="ark-debian-base-$VERSION"

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
    --metadata="serial-port-enable=1" \
    --network="${NETWORK:-default}"${SUBNET:+ --subnet="$SUBNET"}

# Wait for SSH to be ready
echo "Waiting for SSH..."
sleep 30

# 2. Push setup scripts, environment.yml, and the ark source package
gcloud compute scp scripts/setup_ark_host.sh environment.yml "$INSTANCE_NAME":~/ --zone="$ZONE" --project="$PROJECT"
# Upload the ark package so setup_ark_host.sh can pip-install it into ark-base.
gcloud compute scp --recurse ark "$INSTANCE_NAME":~/ark --zone="$ZONE" --project="$PROJECT"
if [ -f "pyproject.toml" ]; then
    gcloud compute scp pyproject.toml "$INSTANCE_NAME":~/ --zone="$ZONE" --project="$PROJECT"
elif [ -f "setup.py" ]; then
    gcloud compute scp setup.py "$INSTANCE_NAME":~/ --zone="$ZONE" --project="$PROJECT"
fi

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
    --family="ark-debian-base"

# 6. Cleanup
echo "Cleaning up temporary instance..."
gcloud compute instances delete "$INSTANCE_NAME" --zone="$ZONE" --project="$PROJECT" --quiet

echo "Successfully built ARK GCP Image: $IMAGE_NAME"
