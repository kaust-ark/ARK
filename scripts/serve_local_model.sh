#!/bin/bash
# Serve a local model on the cluster, OpenAI-compatible, for zero-cost runs.
#
# Why this exists
# ---------------
# Provider credit runs out, and infrastructure work (sandbox changes, runtime
# changes, pipeline plumbing) does not need a frontier model to validate. A
# local endpoint makes that testing free and unlimited, and keeps project data
# on our own hardware.
#
#   sbatch scripts/serve_local_model.sh
#   # then point the pipeline at it:
#   LLM_BASE_URL=http://<node>:8078/v1  model=hosted_vllm/qwen2.5-32b-awq
#
# Four landmines are encoded below. Each cost a debugging round.
#
# 1. DRIVER CEILING. The cluster runs driver 535 (CUDA 12.2). Current vLLM
#    ships a torch that refuses to start on it ("driver is too old"). vLLM
#    0.7.x / torch 2.5.1+cu121 is the working ceiling here. That also caps
#    which model ARCHITECTURES can load: 2026-era models need a newer vLLM,
#    so this box is limited to the Qwen2.5 / Llama-3.x generation until
#    someone upgrades the driver. Ask IT before assuming a model "does not
#    work".
# 2. TRANSFORMERS DRIFT. pip only enforces a lower bound, so a fresh install
#    pairs vLLM 0.7.3 with transformers 5.x, whose tokenizer API it cannot
#    use ("Qwen2Tokenizer has no attribute all_special_tokens_extended").
#    Pin transformers to the same generation as vLLM.
# 3. LD_LIBRARY_PATH. Batch jobs inherit the submitting shell's
#    LD_LIBRARY_PATH, so the node's older system libstdc++ wins over conda's
#    and `import sqlite3` dies on a missing CXXABI. Interactive srun happens
#    to escape this, which makes it look like a batch-only bug. Put the env's
#    lib first, explicitly.
# 4a. THE JOB'S WALL CLOCK IS PART OF THE SLA. An 8h limit sounded generous
#    and then a run launched in hour 7 lost its endpoint mid-experiment
#    ("Connection refused", ba4a1fa7) when Slurm killed the server on
#    schedule. Serve with a horizon longer than any run that might start
#    near the end of it.
# 4b. LEAVE HEADROOM OR DIE MID-SERVE. At --gpu-memory-utilization 0.94 the
#    server STARTS fine and then OOMs under load ("Tried to allocate 1.41 GiB,
#    1.33 GiB free", 2026-08-17 02:49): CUDA-graph pools plus a few concurrent
#    long-context requests claim memory the startup accounting never saw, and
#    the whole engine shuts down — every client sees "Connection refused", not
#    a per-request error. 0.90 plus a bounded --max-num-seqs survives the same
#    load. A dead server fails ALL runs; slightly less KV cache slows them.
# 4. CONTEXT IS BOUNDED BY KV CACHE, NOT WEIGHTS. Per token, KV costs
#    2 * layers * kv_heads * head_dim * 2 bytes: roughly 260 KB/token for a
#    32B model, so 100k tokens of context needs ~26 GB of VRAM ON TOP of the
#    weights. Ask for more than fits and vLLM refuses to start. Quantisation
#    buys context, not just capacity: 32B AWQ leaves far more room on a 40 GB
#    card than 14B BF16 does.
#
# A100 note: Ampere has no FP8, so INT4 (AWQ/GPTQ) is the quantisation that
# actually accelerates here. Do not reach for FP8 builds.
#
# WHAT THIS IS AND IS NOT GOOD FOR
# --------------------------------
# Serving works, and tool calling works: Qwen2.5-32B-AWQ selects correctly among
# multiple tools at 32k context on one A100. Driving it through the real agent
# runtime is a different matter. Asked to write a file under the project, it
# tried to write "/local_ok.txt", the tool returned
#
#     Ran into [Errno 13] Permission denied: '/local_ok.txt'
#
# and it then finished with "The file local_ok.txt has been created with the
# content 'LOCAL-AGENT-WORKS'." It ignored the path boundary and reported
# success over a refused call — in one short task. Treat this endpoint as
# infrastructure test traffic (does the plumbing move bytes, does the sandbox
# hold, does retry classification fire), NOT as a model to produce papers with.
# The false-success guard in ark/engines (rejected tool calls are logged) exists
# because of this run.
#
#SBATCH -p mc
#SBATCH --gres=gpu:a100:1
#SBATCH -c 16
#SBATCH -t 48:00:00
#SBATCH -J ark-vllm
#SBATCH -o %x-%j.out
set -euo pipefail

MODEL="${ARK_LOCAL_MODEL:-Qwen/Qwen2.5-32B-Instruct-AWQ}"
SERVED_NAME="${ARK_LOCAL_MODEL_NAME:-qwen2.5-32b-awq}"
PORT="${ARK_LOCAL_MODEL_PORT:-8078}"
# Qwen2.5's native ceiling. Raising this needs rope scaling, not just a bigger
# number, and vLLM will refuse a value above max_position_embeddings.
MAXLEN="${ARK_LOCAL_MODEL_MAXLEN:-32768}"
ENV_PREFIX="${ARK_VLLM_ENV:-/data/fat/ark/conda/envs/ark-vllm}"

export HF_HOME="${HF_HOME:-$HOME/.cache/huggingface}"
export LD_LIBRARY_PATH="$ENV_PREFIX/lib:${LD_LIBRARY_PATH:-}"   # landmine 3

# THE DRIVER CEILING HAS A USER-SPACE EXIT (measured 2026-08-17). NVIDIA's
# forward-compatibility package puts a newer user-mode libcuda in front of the
# old kernel driver — datacenter GPUs only, no root needed. With
# ARK_VLLM_COMPAT pointing at the extracted libs, torch 2.11+cu128 ran real
# matmuls on driver 535.216.01 (A100, mcnode23). This is what unlocks
# 2026-era vLLM and models WITHOUT waiting for IT; landmine 1 above then only
# applies to envs served without this variable.
if [ -n "${ARK_VLLM_COMPAT:-}" ] && [ -d "${ARK_VLLM_COMPAT}" ]; then
  export LD_LIBRARY_PATH="${ARK_VLLM_COMPAT}:$LD_LIBRARY_PATH"
fi

echo "NODE=$(hostname)  PORT=$PORT  MODEL=$MODEL"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

# 6. THE CODER VARIANT SHIPS A TEMPLATE WITHOUT TOOLS. Qwen2.5-Coder-32B's AWQ
#    repo carries a chat template lacking the tools section, so the model is
#    never told to wrap calls in <tool_call> tags — it emits perfectly-formed
#    tool JSON as plain TEXT, the hermes parser sees nothing, and every agent
#    looks like it "narrates instead of acting". Borrow the Instruct variant's
#    template (same family, tool section included) via ARK_LOCAL_MODEL_TEMPLATE.
TEMPLATE_ARGS=()
if [ -n "${ARK_LOCAL_MODEL_TEMPLATE:-}" ] && [ -f "${ARK_LOCAL_MODEL_TEMPLATE}" ]; then
  TEMPLATE_ARGS=(--chat-template "${ARK_LOCAL_MODEL_TEMPLATE}")
fi

QUANT=()
case "$MODEL" in
  *AWQ*|*awq*)  QUANT=(--quantization awq_marlin) ;;
  *GPTQ*|*Int4*) QUANT=(--quantization gptq_marlin) ;;
esac

exec "$ENV_PREFIX/bin/vllm" serve "$MODEL" \
  --host 0.0.0.0 --port "$PORT" \
  --max-model-len "$MAXLEN" \
  --gpu-memory-utilization 0.90 \
  --max-num-seqs "${ARK_LOCAL_MODEL_MAXSEQS:-8}" \
  --tensor-parallel-size "${ARK_LOCAL_MODEL_TP:-1}" \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --served-model-name "$SERVED_NAME" \
  "${TEMPLATE_ARGS[@]}" \
  "${QUANT[@]}" \
  ${ARK_LOCAL_MODEL_EXTRA_ARGS:-}
