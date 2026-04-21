#!/bin/sh
# =============================================================================
# ARK Job — Docker entrypoint
#
# Sets up the /data directory structure, symlinks the ARK config dir, then
# launches the orchestrator.  All arguments are passed through to
# `python -m ark.orchestrator`.
#
# Example:
#   docker run ark-job \
#     --project myproject \
#     --project-dir /data/projects/myproject \
#     --mode research \
#     --iterations 10
# =============================================================================
set -e

# ── 1. Ensure persistent data directories exist ───────────────────────────────
mkdir -p /data/.ark /data/projects

# ── 2. Symlink /data/.ark → /app/.ark so ark.paths.get_config_dir() resolves ─
if [ ! -e /app/.ark ]; then
    ln -s /data/.ark /app/.ark
    echo "[entrypoint] Linked /app/.ark → /data/.ark"
fi

# ── 3. Validate that a project was supplied ───────────────────────────────────
if [ "$1" = "--help" ] || [ "$#" -eq 0 ]; then
    echo ""
    echo "ARK Job Container"
    echo "─────────────────"
    echo "Usage:"
    echo "  docker run ark-job \\"
    echo "    --project <name> \\"
    echo "    --project-dir /data/projects/<name> \\"
    echo "    --mode research|paper|dev \\"
    echo "    --iterations <n>"
    echo ""
    echo "Required env vars (at least one LLM key):"
    echo "  ANTHROPIC_API_KEY   — for Claude models"
    echo "  OPENAI_API_KEY      — for GPT models"
    echo "  GEMINI_API_KEY      — for Gemini models"
    echo ""
    echo "Optional env vars:"
    echo "  PROJECTS_ROOT       — default: /data/projects"
    echo "  ARK_WEBAPP_DB_PATH  — default: /data/webapp.db (for status sync)"
    echo ""
    python -m ark.orchestrator --help 2>/dev/null || true
    exit 0
fi

# ── 4. Launch the orchestrator ────────────────────────────────────────────────
echo "[entrypoint] Starting ARK orchestrator: python -m ark.orchestrator $*"
exec python -m ark.orchestrator "$@"
