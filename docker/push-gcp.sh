#!/bin/bash
# =============================================================================
# ARK — GCP Push Script
#
# Tags and pushes the ARK Docker images to Google Cloud Artifact Registry
# (or legacy Google Container Registry).
#
# Usage:
#   chmod +x docker/push-gcp.sh
#   ./docker/push-gcp.sh --project [PROJECT_ID] --region [REGION] --repo [REPO]
#
# Examples:
#   # Push to Artifact Registry (recommended)
#   ./docker/push-gcp.sh --project my-gcp-project --region us-central1 --repo ark-repo
#
#   # Push to Legacy Container Registry (gcr.io)
#   ./docker/push-gcp.sh --project my-gcp-project --legacy
# =============================================================================

set -e

# Default values
PROJECT_ID=""
REGION=""
REPO=""
TAG="latest"
LEGACY=false
BUILD=false

# Helper: print usage
usage() {
    echo "Usage: $0 [options]"
    echo ""
    echo "Options:"
    echo "  --project ID   GCP Project ID (required)"
    echo "  --region REG   GCP Region (e.g., us-central1). Required for Artifact Registry."
    echo "  --repo NAME    Artifact Registry repository name. Required for Artifact Registry."
    echo "  --tag TAG      Image tag (default: latest)"
    echo "  --legacy       Push to legacy gcr.io instead of Artifact Registry"
    echo "  --build        Build images for linux/amd64 before pushing"
    echo "  --help         Show this help"
    exit 1
}

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --project) PROJECT_ID="$2"; shift ;;
        --region)  REGION="$2"; shift ;;
        --repo)    REPO="$2"; shift ;;
        --tag)     TAG="$2"; shift ;;
        --legacy)  LEGACY=true ;;
        --build)   BUILD=true ;;
        --help)    usage ;;
        *) echo "Unknown parameter: $1"; usage ;;
    esac
    shift
done

if [ -z "$PROJECT_ID" ]; then
    echo "Error: --project ID is required."
    usage
fi

if [ "$LEGACY" = false ]; then
    if [ -z "$REGION" ] || [ -z "$REPO" ]; then
        echo "Error: --region and --repo are required for Artifact Registry."
        echo "Use --legacy if you want to push to gcr.io instead."
        usage
    fi
    REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}"
    AUTH_HOSTNAME="${REGION}-docker.pkg.dev"
else
    REGISTRY="gcr.io/${PROJECT_ID}"
    AUTH_HOSTNAME="gcr.io"
fi

echo "=== Configuration ==="
echo "Project:  $PROJECT_ID"
if [ "$LEGACY" = false ]; then
    echo "Region:   $REGION"
    echo "Repo:     $REPO"
fi
echo "Registry: $REGISTRY"
echo "Tag:      $TAG"
echo "Build AMD64: $BUILD"
echo "====================="

# 1. Build if requested (Force AMD64)
if [ "$BUILD" = true ]; then
    echo "Building images for linux/amd64..."
    docker build --platform linux/amd64 -f docker/Dockerfile.webapp -t ark-webapp:latest .
    docker build --platform linux/amd64 -f docker/Dockerfile.job -t ark-job:latest .
    echo "Build complete."
fi

# 2. Authenticate
echo "Authenticating with GCP..."
gcloud auth configure-docker "$AUTH_HOSTNAME" --quiet

# 2. Tag and Push Webapp
echo "Processing ark-webapp..."
docker tag ark-webapp:latest "${REGISTRY}/ark-webapp:${TAG}"
docker push "${REGISTRY}/ark-webapp:${TAG}"

# 3. Tag and Push Job
echo "Processing ark-job..."
docker tag ark-job:latest "${REGISTRY}/ark-job:${TAG}"
docker push "${REGISTRY}/ark-job:${TAG}"

echo ""
echo "Successfully pushed images to GCP:"
echo " - ${REGISTRY}/ark-webapp:${TAG}"
echo " - ${REGISTRY}/ark-job:${TAG}"
