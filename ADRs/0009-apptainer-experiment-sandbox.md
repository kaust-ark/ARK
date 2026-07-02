# ADR-0009 — Run agent-generated experiment code in an Apptainer sandbox

- **Status:** Implemented (`feat/byoc-cloud-backend`), cooperative isolation
- **Date:** 2026-07-01
- **Deciders:** ARK core
- **Related:** commit `8eecd6d`; `ark/sandbox.py`, `ark/orchestrator/pipeline.py` (Research Step 0)

## Context

ARK's experimenter/coder agents generate and run arbitrary Python during a run.
Running it directly on the bare host risks polluting the host environment, produces
non-reproducible results (whatever happens to be installed), and gives untrusted code
free run of the machine. Under BYOC this executes in the user's cloud, but the hygiene,
reproducibility, and isolation concerns remain.

## Decision

We will run agent-generated experiment code inside a **prebuilt Apptainer image**
(`.ark/data/_sandbox/ark-sci.sif`: python3.12 + numpy/scipy/sklearn/pandas/matplotlib/
statsmodels):

- `ark/sandbox.py` provides `sandbox_available()`, `write_sandbox_helper()`,
  `experimenter_directive()`.
- Research Step 0 seeds `<project>/sandbox/run.sh`, **fail-open**: it is a no-op if
  Apptainer or the image is missing, so environments without the sandbox keep working.
- Engines' `run_agent` appends the sandbox directive for the experimenter/coder roles,
  so they invoke code via `./sandbox/run.sh .conda_env/bin/python`.

Isolation is currently **cooperative** (agent-driven — the agent is directed to use
the wrapper), **not a hard boundary**. Hardening to an enforced boundary is tracked
separately.

## Consequences

- Host hygiene, reproducibility (pinned scientific stack), and isolation of untrusted
  code improve.
- Fail-open design means no regression where Apptainer isn't installed.
- Because isolation is cooperative, a misbehaving or non-compliant agent can still
  bypass the sandbox — this is a known limitation, not a security guarantee, until the
  hardening follow-up lands.
- Introduces a conda-env-vs-container-python reconciliation that had to be validated
  (smoke test `ae0d0974`: real experiment ran via the sandbox, zero host-fallback).

## Alternatives considered

- **Run experiment code directly on the host.** Rejected: host pollution, non-
  reproducibility, and no isolation of untrusted code.
- **A hard/enforced sandbox boundary now.** Deferred: cooperative wrapping ships the
  hygiene and reproducibility benefits immediately; enforced isolation is tracked as a
  separate hardening task.
