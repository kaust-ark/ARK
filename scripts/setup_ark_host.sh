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

# 2. Conda Environment (Miniforge)
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

# 3. Create ark-base environment
# We expect environment.yml to be in the current dir or we can download it
# For the image builder, we'll assume it's uploaded to the VM.
if [ -f "environment.yml" ]; then
    sudo /opt/conda/bin/conda env create -f environment.yml || true
fi

# 4. Node.js & Claude CLI
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt-get install -y nodejs
sudo npm install -g @anthropic-ai/claude-code

# 5. ARK User and Directories
if ! id "ark" &>/dev/null; then
    sudo useradd -m -s /bin/bash ark
fi
sudo mkdir -p /data/projects /data/.ark
sudo chown -R ark:ark /data /opt/conda

# Add conda to ark user's path
echo 'export PATH="/opt/conda/bin:$PATH"' | sudo tee -a /home/ark/.bashrc
echo 'conda activate ark-base' | sudo tee -a /home/ark/.bashrc

echo "ARK host setup complete."
