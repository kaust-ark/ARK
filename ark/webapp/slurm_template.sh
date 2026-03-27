#!/bin/bash
#SBATCH --job-name=ARK_{{ project_id }}
#SBATCH --output={{ log_dir }}/slurm_%j.out
#SBATCH --error={{ log_dir }}/slurm_%j.err
#SBATCH --time=48:00:00
{% if gres %}#SBATCH --gres={{ gres }}{% endif %}
#SBATCH --cpus-per-task={{ cpus_per_task }}
{% if partition %}#SBATCH --partition={{ partition }}{% endif %}
{% if account %}#SBATCH --account={{ account }}{% endif %}

set -e

echo "[ARK] Job started: $(date)"
echo "[ARK] Project: {{ project_id }}"
echo "[ARK] Project dir: {{ project_dir }}"

source ~/.bashrc
export PATH="$HOME/.local/bin:$HOME/texlive/2025/bin/x86_64-linux:$PATH"
conda activate {{ conda_env }}

cd {{ project_dir }}
python -m ark.orchestrator \
  --project {{ project_id }} \
  --project-dir {{ project_dir }} \
  --code-dir {{ project_dir }} \
  --mode {{ mode }} \
  --iterations {{ max_iterations }} \
  --max-days 2

echo "[ARK] Job finished: $(date)"
