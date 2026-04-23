---
name: rocs-testbed-slurm
description: How to submit SLURM jobs on the SANDS Lab ROCS testbed cluster (KAUST). Covers GPU GRES selection (v100/a100 — p100 is off-policy), QoS caps, conda env activation, multi-node DeepSpeed, preemptible spot jobs, and the watchdog that kills jobs with <15% GPU utilization. Use for any project whose compute_backend is slurm and whose cluster nodes are named `mcnode*`.
tags: [hpc, slurm, rocs-testbed, kaust, gpu, sbatch, conda, mamba]
source: https://sands.kaust.edu.sa/internal/rocs-testbed/slurm-environment/
---

# ROCS Testbed — SLURM Reference

This is the SANDS Lab / KAUST internal cluster. Head node `mcmgt01`, compute nodes `mcnode01…mcnode33`. Different from IBEX — no `module` system, no shared filesystem with IBEX.

## What experimenters MUST get right

1. **Always use `#!/bin/bash --login`** (or `-l`) as the shebang. Without `--login`, `mamba activate` / `conda activate` silently no-ops and the job runs against system Python.
2. **Request GPU explicitly via `--gres`** when the code trains a neural network, runs PyTorch/JAX/TensorFlow, or otherwise benefits from CUDA. Submitting to the `mc` partition without `--gres` gets you a CPU-only allocation on a GPU-equipped node — wasted machine-time and badly slow training.
3. **Pick the weakest GPU that works**: **V100 > A100** (prefer V100). Only escalate to A100 if the model/batch-size genuinely requires it. **Do not use P100** — it exists on the cluster but this project's policy is V100 minimum.
4. **Set `--time` conservatively**. Max job length is 14 days; interactive sessions cap at 4h. Jobs with `--time` > 3 days will soon need to be preemptible.
5. **Don't push GPU utilization below 15%** — any GPU in the job running at <15% for 1h triggers a warning email; 2h consecutive triggers automatic cancellation.

## Minimal GPU sbatch template (copy-paste)

```bash
#!/bin/bash --login
#SBATCH --job-name=<PREFIX>_exp1       # keep the required prefix from ARK config
#SBATCH --partition=mc
#SBATCH --time=04:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --gres=gpu:v100:1              # ← V100 minimum; use a100:1 only if you truly need
#SBATCH --output=results/<exp_dir>/slurm_%j.out
#SBATCH --error=results/<exp_dir>/slurm_%j.err

set -e
mamba activate <project_env>           # or: conda activate .env

python scripts/train.py --config configs/base.yaml
```

`#SBATCH --gres=gpu:v100:1` is the single most important line that experimenters currently forget. **If you do ML and don't include it, the node will be assigned but `cuda.is_available()` returns False and training falls back to CPU — a 20–100× slowdown that can push a 10-minute training run past the pipeline's wait-timeout.**

## CPU-only sbatch template

For pure data processing / backtesting / plotting that doesn't use a GPU:

```bash
#!/bin/bash --login
#SBATCH --job-name=<PREFIX>_exp1
#SBATCH --partition=mc
#SBATCH --time=02:00:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --output=results/<exp_dir>/slurm_%j.out
#SBATCH --error=results/<exp_dir>/slurm_%j.err

set -e
mamba activate <project_env>

python scripts/backtest.py
```

**Do NOT include `--gres=gpu:...` when you don't use GPU** — it burns one of your QoS quota slots (A100=2, V100=8) and blocks a neighbor's real GPU job.

## GPU selection guide

| GPU | When to use | Quota (normal QoS) |
|---|---|---|
| `v100` | **Default** — classical ML, GRU/LSTM, most deep-learning work, moderate-size models (≤200M), mixed-precision | 8 |
| `a100` | Large models (>500M), long training (days), high-memory batches | **2** |

> **Note:** `p100` nodes exist on the cluster (`mcnode01`, `mcnode02`) but are **not in our allowed GPU set** — V100 is the floor. If `sinfo` / `ginfo` shows only P100s free, wait for V100 instead of falling back.

For A100 specifically, add `--constraint=gpu_a100_80gb` or `gpu_a100_40gb` to pin memory capacity, and `--constraint=gpu_sxm` if you need multi-GPU NVLink (PCI A100 inter-GPU bandwidth is ~3× slower, and per-card ~10% slower than SXM).

```bash
#SBATCH --gres=gpu:a100:1
#SBATCH --constraint=gpu_a100_80gb      # or gpu_a100_40gb
#SBATCH --constraint=gpu_sxm            # for multi-GPU; omit for single-card
```

## QoS and preemption

- Default QoS = `normal`. Caps: A100=2, V100=8 per user (concurrent). P100 has a cap of 8 but is off-policy for this project.
- Low-priority QoS = `spot` — no caps, but preemptible by normal-QoS jobs.
- To run a long (>3 day) job, use `--qos=spot` with a checkpoint-and-resume signal handler:

```bash
#SBATCH --signal=R:USR1@60   # SIGUSR1 60s before preemption; R = run on reserved too
#SBATCH --qos=spot
srun python3 train_with_checkpoints.py
```

The ML code must install a SIGUSR1 handler that saves state and exits; SLURM will then auto-requeue the job (same JobID) on a free resource. See the `signal.signal(SIGUSR1, ...)` pattern in the ROCS docs.

## Environment activation — the trap

Without `--login` shebang:
```bash
#!/bin/bash                  # ← WRONG — mamba activate silently no-ops
mamba activate my-env
python train.py              # runs against system Python, not my-env
```

Correct:
```bash
#!/bin/bash --login          # ← required
mamba activate my-env
python train.py              # picks up the right interpreter
```

The `--login` flag sources `~/.bashrc` / `~/.bash_profile`, which the mamba/conda init blocks into; without it, `mamba` isn't even on PATH in the batch shell.

## Job lifecycle commands (cluster-specific shortcuts)

| Command | Purpose |
|---|---|
| `sbatch script.slurm` | Submit batch job |
| `squeue --me` | Your queued/running jobs only |
| `scancel --me` | Kill all your jobs |
| `srecent` | Recent jobs table (alias for `sacct -X -o "JobID,Start,End,Elapsed,JobName,AllocTRES,NodeList,State,ExitCode"`) |
| `jobstats <jobid>` | Text summary (retained forever) |
| `jobstats <jobid> -g` | Grafana dashboard URL (creds `rocs/rocs`, data retained 15d) |
| `ninfo` | Per-node features + GRES (find which nodes have your GPU type) |
| `ginfo` | GPU pool utilization snapshot |
| `sinfo` | Partitions + node state |

`scontrol show job <jobid>` gives allocation details including `StdOut=` / `StdErr=` paths, useful for the ARK SLURM watcher to poll for progress.

## Multi-node / DeepSpeed template

Only use if your training genuinely spans >1 node. Prefer single-node unless memory or throughput demands more. Full template in the upstream docs; the essentials:

```bash
#!/bin/bash -l
#SBATCH --job-name=ds_exp
#SBATCH --ntasks-per-node=1              # 1 launcher task per node — workers are spawned by torchrun
#SBATCH --cpus-per-gpu=4
#SBATCH --mem-per-gpu=40GB
#SBATCH --nodes=2
#SBATCH --gpus-per-node=a100:1

set -e
mamba activate deepspeed-env

export NCCL_SOCKET_IFNAME=fabric         # cluster's 100 Gbps internal network
export NCCL_DEBUG=INFO

MASTER_ADDR=$(scontrol show hostnames $SLURM_JOB_NODELIST | head -n 1)
MASTER_PORT=$((${SLURM_JOB_ID} % 16384 + 49152))

srun --wait=60 --kill-on-bad-exit=1 \
     torchrun --nproc_per_node gpu \
              --nnodes $SLURM_NNODES \
              --rdzv_endpoint $MASTER_ADDR:$MASTER_PORT \
              --rdzv_backend c10d --tee 3 \
              train.py --deepspeed --deepspeed_config ds_config.json
```

## Watchdog policies — jobs that get auto-killed

The cluster's `job_defense_shield` cancels jobs exhibiting waste. For an ARK experimenter this matters because getting cancelled halfway through a backtest wastes the whole run.

- **Low GPU utilization**: any GPU in your job running at <15% for 1h → warning email. Persists 2h → cancelled.
- **Common cause**: allocated GPU but CPU-bound data loading, or forgot to move tensors to `cuda`.
- **Fix**: if your code is CPU-heavy, don't request GPU; if it should use GPU, verify `tensor.to("cuda")` and `pin_memory=True` in DataLoader.

## Checklist before submitting any sbatch

- [ ] Shebang is `#!/bin/bash --login`
- [ ] Job name starts with the ARK project prefix (so the pipeline's wait-loop can find it)
- [ ] `--time=` is set, reasonable (not "forever")
- [ ] If training: `--gres=gpu:<type>:N` with the **weakest** GPU that works
- [ ] If NOT training: no `--gres=gpu` line
- [ ] `mamba activate <env>` (or conda) to get the right Python
- [ ] `set -e` so failures propagate as non-zero exit
- [ ] `--output=` / `--error=` point to the experiment's results dir so outputs are discoverable
- [ ] For long jobs (>3 days): `--qos=spot` + SIGUSR1 handler

## Verifying a GPU job actually got a GPU

After a job starts, check:

```bash
scontrol show job <jobid> | grep -E "TRES|NodeList"
# Expect: gres/gpu:v100=1  (or a100)
# If you see only cpu= and mem=, you forgot --gres
```

Or from inside the sbatch script, log it:
```bash
python -c "import torch; print('cuda_available:', torch.cuda.is_available(), 'device_count:', torch.cuda.device_count())"
```

## When to contact the cluster admins

- Sustained resource needs beyond QoS caps → open a "GPU Reservation Request" issue at `https://github.com/sands-lab/rocs-testbed/issues`. Decisions take ~1 week.
- Unexpected cancellations with `State=CANCELLED BY <uid>` where uid is NOT your own → that was a watchdog or admin action; `jobstats <jobid>` shows the reason.

## References

- Upstream docs (source for this skill): https://sands.kaust.edu.sa/internal/rocs-testbed/slurm-environment/
- SLURM sbatch options: https://slurm.schedmd.com/sbatch.html
- PyTorch checkpointing: https://docs.pytorch.org/tutorials/beginner/saving_loading_models.html
- PyTorch Lightning signal handlers: https://lightning.ai/docs/pytorch/stable/common/checkpointing.html
