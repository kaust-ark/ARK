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
trap 'rm -rf "{{ project_dir }}/.gemini" "{{ project_dir }}/.config" "{{ project_dir }}/.claude.json"' EXIT TERM INT

echo "[ARK] Job started: $(date)"
echo "[ARK] Project: {{ project_id }}"
echo "[ARK] Project dir: {{ project_dir }}"

source ~/.bashrc
# Shared team locations first (ARK_TOOLS_BIN / ARK_TEXLIVE_BIN propagate via
# sbatch's exported environment), then per-user fallbacks.
export PATH="${ARK_TOOLS_BIN:+$ARK_TOOLS_BIN:}${ARK_TEXLIVE_BIN:+$ARK_TEXLIVE_BIN:}$HOME/.local/bin:$HOME/texlive/2025/bin/x86_64-linux:$PATH"
# On a team deployment, ARK_CONDA_ROOT propagates via sbatch's exported
# environment — make its conda function and shared envs available so the
# fallback env resolves even when the user's personal conda lacks it.
if [ -n "${ARK_CONDA_ROOT:-}" ] && [ -f "$ARK_CONDA_ROOT/etc/profile.d/conda.sh" ]; then
    source "$ARK_CONDA_ROOT/etc/profile.d/conda.sh"
fi
# Prefer the project-local conda env (created at submission time);
# fall back to the shared {{ conda_env }} env for legacy projects.
if [ -d "{{ project_dir }}/.env/conda-meta" ]; then
    conda activate "{{ project_dir }}/.env"
elif [ -n "${ARK_CONDA_ROOT:-}" ] && [ -d "$ARK_CONDA_ROOT/envs/{{ conda_env }}" ]; then
    conda activate "$ARK_CONDA_ROOT/envs/{{ conda_env }}"
else
    conda activate {{ conda_env }}
fi
{# Credential env comes pre-mapped from jobs.api_keys_to_env — the single
   source of truth. The old inline whitelist here silently dropped OpenRouter
   and every long-tail provider (deepseek, xai, …). #}
{% for k, v in (api_env | default({})).items() %}
export {{ k }}={{ v }}
{% endfor %}
{% if control_plane_url %}
# Control-plane bearer token via env (not argv) so it never shows in `ps`.
export ARK_CONTROL_PLANE_TOKEN={{ control_plane_token }}
{% endif %}
export HOME="{{ project_dir }}"
export XDG_CONFIG_HOME="{{ project_dir }}/.config"
# Disable user-site discovery so the project's conda env is the only
# source of Python packages. No /home/<user>/.local cross-contamination.
export PYTHONNOUSERSITE=1
# Inject the launching webapp's source so `python -m ark.orchestrator` finds
# ark even though the cloned project env (and ark-base) deliberately omit it.
# This mirrors what launch_local_job sets in jobs.py.
export PYTHONPATH="{{ ark_code_root }}${PYTHONPATH:+:$PYTHONPATH}"

cd {{ project_dir }}
python -m ark.orchestrator \
  --project {{ project_id }} \
  --project-dir {{ project_dir }} \
  --code-dir {{ project_dir }} \
  --mode {{ mode }} \
  --iterations {{ max_iterations }} \
  --max-days 2 \
  {% if control_plane_url %}--control-plane-url {{ control_plane_url }}{% else %}--db-path {{ db_path }}{% endif %} \
  --project-id {{ project_id }}

echo "[ARK] Job finished: $(date)"
