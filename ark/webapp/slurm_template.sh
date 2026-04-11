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
# Prefer the project-local conda env (created at submission time);
# fall back to the shared {{ conda_env }} env for legacy projects.
if [ -d "{{ project_dir }}/.env/conda-meta" ]; then
    conda activate "{{ project_dir }}/.env"
else
    conda activate {{ conda_env }}
fi
{% for k, v in api_keys.items() %}
{% if k == "claude_oauth_token" %}
export CLAUDE_CODE_OAUTH_TOKEN={{ v }}
{% elif k.endswith("_api_key") or k in ("gemini", "anthropic", "openai") %}
{% set env_key = k.upper() ~ "_API_KEY" if "_api_key" not in k.lower() else k.upper() %}
export {{ env_key }}={{ v }}
{% endif %}
{% endfor %}
export HOME="{{ project_dir }}"
export XDG_CONFIG_HOME="{{ project_dir }}/.config"
# Disable user-site discovery so the project's conda env is the only
# source of Python packages. No /home/<user>/.local cross-contamination.
export PYTHONNOUSERSITE=1

cd {{ project_dir }}
python -m ark.orchestrator \
  --project {{ project_id }} \
  --project-dir {{ project_dir }} \
  --code-dir {{ project_dir }} \
  --mode {{ mode }} \
  --iterations {{ max_iterations }} \
  --max-days 2

echo "[ARK] Job finished: $(date)"
