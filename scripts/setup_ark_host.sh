#!/bin/bash
# =============================================================================
# ARK Host Setup Script
#
# This script provisions a Debian-based VM with all dependencies required to
# run the ARK Orchestrator and experiments directly on the host.
# Mirror of docker/Dockerfile.job for bare-metal/VM execution.
# =============================================================================

set -e
set -x

# 1. System dependencies
# texlive-full (not a curated subset) matches the SkyPilot setup: block and
# ARK's own recommendation (latex_utils.detect_latex_install_command → texlive-full),
# so a baked image can't hit a missing-package compile failure the live setup wouldn't.
# Heavy (~GBs) but paid once at bake time. latexmk/biber are already in texlive-full;
# kept explicit for clarity.
sudo apt-get update && sudo apt-get install -y --no-install-recommends \
    texlive-full \
    latexmk \
    biber \
    pandoc \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    git \
    git-lfs \
    build-essential \
    curl \
    wget \
    unzip \
    rsync \
    openssh-client

sudo git lfs install

# 2. Google Cloud SDK
if ! command -v gcloud &>/dev/null; then
    curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list
    sudo apt-get update && sudo apt-get install -y google-cloud-sdk
fi

# 3. Conda Environment (Miniforge)
if [ ! -d "/opt/conda" ]; then
    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64)  MINIFORGE_ARCH="x86_64" ;;
        aarch64) MINIFORGE_ARCH="aarch64" ;;
        *) echo "Unsupported architecture: $ARCH"; exit 1 ;;
    esac
    wget -qO /tmp/miniforge.sh "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-${MINIFORGE_ARCH}.sh"
    sudo bash /tmp/miniforge.sh -b -p /opt/conda
    rm /tmp/miniforge.sh
    sudo ln -sf /opt/conda/bin/conda /usr/local/bin/conda
fi

# 4. Create ark-base environment from environment.yml, then install ark itself.
# environment.yml declares all third-party deps (anthropic, openai, etc.).
# ark is a local package synced at runtime by OrchestratorCloudBackend.run_orchestrator();
# install it here in editable mode using a temporary checkout so the image is
# self-contained and doesn't rely solely on PYTHONPATH being forwarded.
if [ -f "environment.yml" ]; then
    sudo /opt/conda/bin/conda env create -f environment.yml || true
fi

# Install the ark package into the env.
# At image-build time the ark source is uploaded alongside setup_ark_host.sh
# by build_ark_gcp_image.sh (see step 2 in that script). If the source directory
# is present, install it; otherwise skip — run_orchestrator will inject PYTHONPATH.
if [ -d "ark" ] && [ -f "pyproject.toml" -o -f "setup.py" ]; then
    echo "Installing ark package (with research extra) into ark-base env..."
    # The [research] extra matches the SkyPilot setup: block
    # (`pip install -e '.[research]'`): google-genai / anthropic / openai /
    # aiofiles etc. must resolve in the same interpreter that runs the
    # orchestrator, or PaperBanana's inline imports silently fall back.
    sudo /opt/conda/envs/ark-base/bin/pip install '.[research]' --no-build-isolation -q
fi

# 5. Node.js & Claude CLI
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt-get install -y nodejs
sudo npm install -g @anthropic-ai/claude-code @google/gemini-cli

# 5b. openhands agent runtime (SkyPilot setup: block parity)
# The orchestrator shells out to the `openhands` binary; ark/pipeline.py fails
# fast without it. It is a separate uv-managed CLI, NOT a pip dep of ark, so it
# is absent from environment.yml — the single biggest gap between this image and
# the live SkyPilot setup block. Install uv and the tool into GLOBAL locations
# (/usr/local/bin, already on the default PATH) so `openhands` resolves for any
# SSH user and any non-interactive shell, independent of conda activation.
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
sudo env UV_TOOL_DIR=/opt/uv/tools UV_TOOL_BIN_DIR=/usr/local/bin \
    /usr/local/bin/uv tool install --python 3.12 openhands

# 6. Directories and conda path for ubuntu user
sudo mkdir -p /data/projects /data/.ark

# Create ubuntu user if it doesn't exist (image builds on GCP Debian don't have it)
if ! id ubuntu &>/dev/null; then
    sudo useradd -m -s /bin/bash ubuntu
    echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/ubuntu-nopasswd
fi

sudo chown -R ubuntu:ubuntu /data /opt/conda

# The raw-gcloud `cloud` backend SSHes in as `ubuntu` and expects ark-base active.
echo 'export PATH="/opt/conda/bin:$PATH"' | sudo tee -a /home/ubuntu/.bashrc
echo 'conda activate ark-base' | sudo tee -a /home/ubuntu/.bashrc

# --- SkyPilot path note (verified against a live GCP probe, 2026-07) -----------
# When this image is used as a SkyPilot `image_id`, SkyPilot does NOT reuse the
# `ubuntu`/`/opt/conda` environment set up above:
#   * it SSHes in as `gcpuser` (not `ubuntu`), so the ubuntu ~/.bashrc lines above
#     don't apply;
#   * it installs its OWN miniconda at ~/miniconda3 and runs the orchestrator's
#     `python -m ark.orchestrator` from THAT base env — so ark must be pip-installed
#     into SkyPilot's runtime conda by the launcher `setup:` block; a baked
#     /opt/conda ark install is invisible to it;
#   * `setup:`/`run:` blocks execute in an INTERACTIVE NON-LOGIN shell, which
#     sources /etc/bash.bashrc and ~/.bashrc but NOT /etc/profile.d.
# What the image therefore contributes to the SkyPilot path is the *system-level*
# deps that live on the default PATH for any user — texlive-full, pandoc, the
# LaTeX/cairo/pango libs (above), the node CLIs, and `openhands` in /usr/local/bin
# (verified on gcpuser's PATH). Those remove the slowest apt/toolchain steps from
# the setup block; the ark pip-install + ark-base conda env still run at setup time.

echo "ARK host setup complete."
