#!/bin/bash
# =============================================================================
# ARK Job — S3-capable Entrypoint (Kubernetes/EKS)
#
# This script handles the "Storage Bridge" between the webapp and the Job pod.
# 1. Download project data from S3 (input)
# 2. Run the ARK orchestrator
# 3. Upload results/ and auto_research/ back to S3 (output)
# =============================================================================
set -e

echo "[ark-job] Starting entrypoint"

# ── 1. Setup local data structure ─────────────────────────────────────────────
# We use /data as the working base. If not mounted via PVC, it's ephemeral.
mkdir -p /data/.ark /data/projects
if [ ! -e /app/.ark ]; then
    ln -s /data/.ark /app/.ark
    echo "[ark-job] Linked /app/.ark → /data/.ark"
fi

# ── 2. Download from S3 (Input Phase) ─────────────────────────────────────────
if [ -n "$ARK_S3_BUCKET" ] && [ -n "$ARK_S3_INPUT_PREFIX" ]; then
    echo "[ark-job] S3 Storage Bridge detected (Input)"
    echo "[ark-job] Downloading s3://$ARK_S3_BUCKET/$ARK_S3_INPUT_PREFIX/ → /data/projects/$ARK_PROJECT_ID/"
    
    mkdir -p "/data/projects/$ARK_PROJECT_ID"
    # Sync from S3 to local project dir
    aws s3 sync "s3://$ARK_S3_BUCKET/$ARK_S3_INPUT_PREFIX" "/data/projects/$ARK_PROJECT_ID" \
        --quiet --no-progress
    
    echo "[ark-job] Download complete"
else
    echo "[ark-job] No S3 input configured. Assuming local data is pre-mounted at /data/projects/"
fi

# ── 3. Run orchestrator ───────────────────────────────────────────────────────
echo "[ark-job] Launching orchestrator..."
echo "[ark-job] Project: $ARK_PROJECT_ID, Mode: $ARK_MODE, Iterations: $ARK_MAX_ITERATIONS"

# Note: We don't use 'exec' here because we need to run the upload phase after
python -m ark.orchestrator \
    --project "$ARK_PROJECT_ID" \
    --project-dir "/data/projects/$ARK_PROJECT_ID" \
    --code-dir "/data/projects/$ARK_PROJECT_ID" \
    --mode "$ARK_MODE" \
    --iterations "$ARK_MAX_ITERATIONS" \
    --project-id "$ARK_PROJECT_ID" \
    2>&1 | tee "/data/projects/$ARK_PROJECT_ID/orchestrator.log"

EXIT_CODE=${PIPESTATUS[0]}
echo "[ark-job] Orchestrator exited with code $EXIT_CODE"

# ── 4. Upload to S3 (Output Phase) ────────────────────────────────────────────
if [ -n "$ARK_S3_BUCKET" ] && [ -n "$ARK_S3_OUTPUT_PREFIX" ]; then
    echo "[ark-job] S3 Storage Bridge (Output)"
    echo "[ark-job] Uploading results/ and auto_research/ to s3://$ARK_S3_BUCKET/$ARK_S3_OUTPUT_PREFIX/"
    
    PROJECT_DIR="/data/projects/$ARK_PROJECT_ID"
    
    # Upload crucial state and results
    # We use 'sync' to avoid redundant uploads and ensure structure is preserved
    for dir in "results" "auto_research" "paper" "logs"; do
        if [ -d "$PROJECT_DIR/$dir" ]; then
            echo "[ark-job] Uploading $dir..."
            aws s3 sync "$PROJECT_DIR/$dir" "s3://$ARK_S3_BUCKET/$ARK_S3_OUTPUT_PREFIX/$dir" \
                --quiet --no-progress
        fi
    done
    
    # Upload the main log file explicitly if it exists
    if [ -f "$PROJECT_DIR/orchestrator.log" ]; then
        aws s3 cp "$PROJECT_DIR/orchestrator.log" "s3://$ARK_S3_BUCKET/$ARK_S3_OUTPUT_PREFIX/orchestrator.log" \
            --quiet
    fi

    echo "[ark-job] Upload complete"
fi

exit $EXIT_CODE
