# Container end-to-end boundary test

Proves the Phase 1 control-plane boundary (`CONTROL_PLANE_BOUNDARY.md` §8) with a
**real orchestrator process in its own container**, talking to the **real webapp**
over the `/v1` HTTP API — **no shared database or filesystem**. This is the gap
the existing tests can't cover:

| Test | What it runs | Boundary realism |
|---|---|---|
| `tests/integration/test_control_plane_api.py` | uvicorn **in-process** + stdlib client | real socket, same process |
| `scripts/smoke_control_plane.py` | drives the client, but **imports `db` directly** to seed/answer | same process/env |
| **this** | real `ark webapp` container ⇄ real `python -m ark.orchestrator` container | **separate containers, network-only** |

## What is real vs. mocked

Everything is the shipping code **except the LLM**. The orchestrator, its agent
engine (`OpenHandsCLI`), LaTeX, git, PyMuPDF, the `HttpControlPlaneClient`, the
`/v1` server, token auth, and the event store all run for real. The only mock is
`fake_openhands.py`, installed as `/app/bin/openhands` (ahead of the real binary
on `PATH`) — so `build_command`/`build_env`/the subprocess stream/`parse_output`
all execute; only the LLM-backed agent loop returns canned output. It's a
standalone port of `tests/conftest.py::MockController`.

## Run it

```bash
scripts/e2e_boundary.sh                 # loop mode (default): 1 real iteration
E2E_MODE=apply scripts/e2e_boundary.sh  # faster: one read-only agent, no loop
KEEP=1   scripts/e2e_boundary.sh        # leave containers up to inspect
NOBUILD=1 scripts/e2e_boundary.sh       # reuse already-built images
```

First run builds two heavy images (`ark-job` base ≈ conda + full TeX + OpenHands,
plus `ark-webapp`) — expect 15–30 min. Subsequent runs reuse them (`NOBUILD=1`).

## Flow (`scripts/e2e_boundary.sh`)

1. **build** `ark-webapp` + `ark-job-e2e` (FROM `ark-job` + fake `openhands`).
2. **up** the `webapp` service; wait for `/dashboard/health`.
3. **seed** (`seed_project.py`, inside webapp): create a project, enqueue a
   non-disruptive `set_autonomy` command, mint a per-run scoped bearer token.
4. **run** (`docker compose run job …`): the isolated orchestrator boots over
   `/v1`, runs, and reports back — token via `-e ARK_CONTROL_PLANE_TOKEN`.
5. **assert** (`assert_boundary.py`, inside webapp): from the control plane's
   side, status reached terminal, live-log **events** landed, the seeded command
   was pulled+acked and applied (loop mode), and a wrong-project token gets 403.
6. **down -v** (unless `KEEP=1`).

## Files

- `fake_openhands.py` — the only mock; canned OpenHands JSONL + side-effect files.
- `project/` — minimal valid project scaffold (config, agent prompts, `main.tex`).
- `seed_project.py` — create project + command + token (runs in webapp container).
- `assert_boundary.py` — verify the crossings landed (runs in webapp container).
- `../../docker/Dockerfile.job.e2e`, `../../docker/docker-compose.e2e.yml`,
  `../../scripts/e2e_boundary.sh` — the image, topology, and runner.

## Not covered here (by design)

HITL decision fan-out over the live Telegram/webapp stack is exercised by
`scripts/smoke_control_plane.py` and `tests/unit/test_controlplane_hitl.py`; a
short deterministic run can't reliably trigger a real decision. Wiring this into
CI is a deliberate follow-up (see the branch's boundary doc).
