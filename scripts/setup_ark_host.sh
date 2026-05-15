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
sudo apt-get update && sudo apt-get install -y --no-install-recommends \
    texlive-latex-base \
    texlive-latex-extra \
    texlive-latex-recommended \
    texlive-science \
    texlive-fonts-recommended \
    texlive-fonts-extra \
    texlive-bibtex-extra \
    texlive-lang-cjk \
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
    echo "Installing ark package into ark-base env..."
    sudo /opt/conda/envs/ark-base/bin/pip install . --no-build-isolation -q
fi

# 5. Node.js & Claude CLI
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt-get install -y nodejs
sudo npm install -g @anthropic-ai/claude-code @google/gemini-cli

# 6. Directories and conda path for ubuntu user
sudo mkdir -p /data/projects /data/.ark

# Create ubuntu user if it doesn't exist (image builds on GCP Debian don't have it)
if ! id ubuntu &>/dev/null; then
    sudo useradd -m -s /bin/bash ubuntu
    echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/ubuntu-nopasswd
fi

sudo chown -R ubuntu:ubuntu /data /opt/conda

echo 'export PATH="/opt/conda/bin:$PATH"' | sudo tee -a /home/ubuntu/.bashrc
echo 'conda activate ark-base' | sudo tee -a /home/ubuntu/.bashrc

echo "ARK host setup complete."
