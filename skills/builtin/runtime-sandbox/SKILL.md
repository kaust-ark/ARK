---
name: runtime-sandbox
description: Containerized isolation via Apptainer for reproducible experiments. Use whenever the project executes third-party code (agent frameworks, browser agents, codegen tools, benchmark harnesses), runs GPU workloads via Slurm on a shared cluster, needs a reproducible runtime environment for paper claims, or isolates untrusted/adversarial inputs from the host. Broadly applicable to measurement, benchmark, and systems papers — not only to malicious-code experiments.
tags: [system, infrastructure, sandbox, apptainer, slurm, gpu, runtime, isolation, reproducibility, third-party-execution, benchmarking]
---

# Runtime Sandbox

## When to Use

Select this skill when the Experimental Protocol involves any of the
following — these cover a broad class of measurement, benchmark, and
systems papers, not only adversarial/malicious experiments:

- **Executing third-party code you don't fully control** — any agent
  framework (OpenClaw, AutoGPT, LangChain, browser-use, AutoGen, codegen
  agents), any benchmark harness that shells out, any evaluation that
  invokes external CLIs. Sandbox keeps the host clean and makes the run
  reproducible across machines.
- **GPU workloads via Slurm** — model serving (Ollama, vLLM, TGI), training,
  large-scale inference. On HPC clusters Docker is typically unavailable;
  Apptainer is the standard rootless alternative with native `--nv` GPU
  passthrough.
- **Reproducibility claims in the paper** — anything saying "evaluated in
  a containerized environment", "deployed on node X", "runs on pinned
  dependency set", or "reproducible via this .sif". The reviewer expects
  the artifact to boot elsewhere and reproduce the numbers.
- **Shared-home / NFS clusters** — when `$HOME` is shared across nodes,
  containerization prevents cross-user env pollution and makes per-project
  Python/Node/CUDA versions independent.
- **Untrusted or adversarial inputs** — malicious skills, jailbreak
  corpora, adversarial prompts, red-team payloads. Isolation is defense
  in depth on top of any software-level firewall.
- **Runtime-overhead measurement** — comparing latency with vs. without
  an intercepting / instrumentation layer.

## When NOT to Use

Skip this skill (prefer running on the host directly) when all of the
following hold:

- The experiment is pure data analysis on local files (pandas / numpy /
  statsmodels) with no third-party agent execution
- No GPU or Slurm involvement
- No paper claim of "sandboxed" / "containerized" / "deployed on X"
- No third-party CLI being invoked whose side effects could leak into
  the host

If any one of the "When to Use" cases applies, prefer this skill over
bare-host execution. The apptainer build is a one-time 20–30 min cost
that pays back in reproducibility and host hygiene for the rest of the
project.

## Why Apptainer (not Docker)

Cluster environments restrict Docker because the daemon needs root. Apptainer
(formerly Singularity) is the standard rootless alternative on HPC and works
without admin privileges. Picks:

- **Apptainer** — rootless, single-file `.sif` image, native Slurm + GPU
  (`--nv`) integration, NFS-friendly. **Default choice.**
- Docker — only when the host explicitly grants Docker access AND the
  runtime is not Slurm. Most research clusters won't allow this.
- Podman — rootless Docker-compatible; useful when you need Docker-Compose-
  style multi-container setups. Niche.

## Quick start: build and run a sandbox

```bash
# 1. Verify Apptainer is available (typical install paths)
command -v apptainer || ls /data/secure/bin/apptainer || \
  echo "Apptainer missing — request via needs_human.json"

# 2. Verify rootless support (kernel must allow user namespaces)
cat /proc/sys/kernel/unprivileged_userns_clone   # must be 1

# 3. Write a .def file (template below)

# 4. Build the .sif (one-time, 5-30 min depending on contents)
apptainer build --fakeroot project.sif project.def

# 5. Run interactively or via Slurm (templates below)
apptainer run --nv --writable-tmpfs --bind ./out:/out project.sif
```

## .def template

```
Bootstrap: docker
From: python:3.12-slim

%post
    set -e
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg build-essential git xz-utils zstd

    # If the project needs Node.js (e.g. for openclaw):
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y --no-install-recommends nodejs

    # Python deps (let pip resolve transitive constraints — pinning fastapi
    # below 0.118 conflicts with nemoguardrails 0.21.0 starlette requirement)
    pip install --no-cache-dir 'nemoguardrails==0.21.0' \
        'fastapi>=0.118.0' 'uvicorn>=0.32.0' httpx pydantic openai

    # Project-specific install (npm, git clone, etc.)
    # cd /opt && git clone <repo> ...

    # Cleanup
    apt-get clean && rm -rf /var/lib/apt/lists/*

%files
    your_code        /opt/your_code
    run_inside.sh    /opt/run_inside.sh

%environment
    export PYTHONUNBUFFERED=1

%runscript
    cd /opt
    exec bash /opt/run_inside.sh "$@"
```

## Slurm submission template

```bash
#!/bin/bash
#SBATCH --job-name=myproject
#SBATCH --partition=mc                    # cluster-specific
#SBATCH --gres=gpu:v100:1                 # or gpu:a100:1 / gpu:p100:1
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --time=02:00:00
#SBATCH --output=logs/slurm_%j.out
#SBATCH --error=logs/slurm_%j.err

set -eo pipefail
SIF=/path/to/project.sif
OUT_DIR=$PWD/slurm_out_$SLURM_JOB_ID
mkdir -p "$OUT_DIR"

apptainer run \
    --nv \                                # GPU passthrough (omit if CPU-only)
    --writable-tmpfs \                    # let in-image processes write to /
    --bind "$OUT_DIR:/out" \              # captured outputs survive
    --bind "./live_code:/opt/code" \      # OPTIONAL: live-edit without rebuild
    "$SIF"
```

## Critical gotchas

These all bit me on real projects — bake them into your runner from the start:

### 1. `--writable-tmpfs` is almost always needed

The .sif rootfs is read-only. Many third-party tools insist on writing
inside their own install dir (e.g. `~/.npm`, `/opt/<framework>/.config`)
even when you set `HOME` elsewhere. `--writable-tmpfs` mounts a tmpfs
overlay so those writes succeed (data lost on container exit, which is
fine — your real outputs go through `--bind /out`).

Symptom of missing this flag: `ENOENT: no such file or directory,
mkdir '/opt/<x>/.config'` (Node.js misreports EROFS as ENOENT).

### 2. `HOME` doesn't always win — set provider-specific env too

Inside Apptainer, `$HOME` defaults to your host home (bind-mounted
read-only). Even if you `export HOME=/out/myhome`, some frameworks read
their own env (`OPENCLAW_STATE_DIR`, `XDG_CONFIG_HOME`, npm cache dirs)
that fall back to package-install paths. Set them explicitly:

```bash
export HOME="$OUT_DIR/home"
export XDG_CONFIG_HOME="$HOME/.config"
export OPENCLAW_STATE_DIR="$HOME/.openclaw-myrun"   # framework-specific
mkdir -p "$HOME" "$XDG_CONFIG_HOME"
```

### 3. `--nv` for GPU, but verify the GPU is actually visible

`--nv` mounts the host's CUDA drivers but does NOT guarantee the device
is allocated. Inside the container, run `nvidia-smi -L` first — if it
fails, your Slurm `--gres` is wrong or the job got CPU-only.

### 4. Bind-mount your scripts during dev iteration

Rebuilding the .sif takes 5-30 min. Bind-mount the scripts you're
iterating on:

```bash
apptainer run \
    --bind ./proxy.py:/opt/code/proxy.py \
    --bind ./policy.co:/opt/code/policy.co \
    "$SIF"
```

Edit on host, save, re-run — no rebuild. Once stable, rebuild the .sif
with the final scripts in `%files`.

### 5. `--net none` blocks egress for malicious-code experiments

When running untrusted skills/agents that may attempt outbound calls,
add `--net none`. Combined with `--writable-tmpfs` it gives you a
fully-isolated sandbox even if the firewall layer fails — defense in
depth. Caveat: any pre-pull of models/packages must happen during build
or before `--net none` is applied.

## Runtime-overhead measurement

If the Protocol asks for "runtime overhead of <intercepting layer>",
run two configs and report the delta:

```python
# baseline.py — direct, no proxy
agent.serve(ollama_endpoint="http://127.0.0.1:11434")

# instrumented.py — through the layer under test
agent.serve(ollama_endpoint="http://127.0.0.1:18080")  # → proxy → ollama

# Compare per-request wall time (mean, median, p95). Report ms delta.
```

Collect ≥ 30 requests per config to get usable confidence. Without a
baseline measurement, "we measured runtime overhead" is unsupported.

## Verifying the sandbox actually ran (paper-evidence)

When `coverage:` claims `sandboxed_environment: status: done`, the
evidence file should show concrete proof that the workload ran inside
the sandbox, not on the host. Useful artifacts:

- `slurm_<job>.out` showing `nvidia-smi -L` output (proves GPU node)
- `runner.log` showing `Apptainer container started at <time>`
- A timestamp inside the container (e.g. `date -u` written to `/out/`)
- `proxy.log` with `PASSTHROUGH POST /api/chat` lines (proves traffic
  routed through the in-container interceptor)

Don't claim sandbox without one of these.

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `ENOENT: mkdir '/opt/...'` | RO rootfs | Add `--writable-tmpfs` |
| `No API key found for provider <X>` from Node CLI | Auth profile path mismatch | Set `<TOOL>_STATE_DIR` explicitly |
| `Model context window too small` | Provider config has stale ctxWindow | Update `models.providers.<x>.models[*].contextWindow` to >= 16000 |
| Slurm job goes PD with `BadConstraints` / `QOSMaxGRESPerUser` | Account quota or wrong gres syntax | Try alt GPU type (a100→v100→p100); check `sacctmgr show user` |
| Build fails with `pip ResolutionImpossible` | Pinned versions conflict | Loosen to `>=` constraints, let pip resolve |
| `unable to extract zst` in ollama install | Missing zstd | Add `zstd` to apt-get install line |
