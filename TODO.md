# idea2paper TODO & Known Issues

## Recently Completed (v0.2)

### [x] Per-project conda environment isolation
- Each project gets its own `.env/` conda env, cloned from a base env
- Sandboxed HOME, `PYTHONNOUSERSITE=1`, isolated PYTHONPATH
- Both CLI (`ark run`) and Web Portal auto-detect and use the project env
- Pipeline bootstrap (Research Phase Step 2) auto-provisions if missing

### [x] 4-step Research Phase pipeline
- Deep Research → Initializer → Planner → Experimenter
- Initializer agent bootstraps env, skills, and citations

### [x] Skills system with 5 builtin skills
- research-integrity, human-intervention, env-isolation, figure-integrity, page-adjustment
- Auto-installed during pipeline bootstrap

### [x] Anti-simulation / anti-shortcut enforcement
- Prompts prevent agents from fabricating experiment results
- Hardened across experimenter, planner, and writer agents

### [x] Human intervention protocol
- Agents escalate decisions to user via Telegram before irreversible actions

### [x] DB as source of truth
- SQLite stores project config, status, scores, costs
- CLI and webapp unified on same DB
- YAML reserved for per-agent runtime state only

### [x] Telegram rich notifications + HPC SSL
- Formatted messages with score changes, phase transitions, agent activity
- Self-signed certificate support for enterprise/HPC networks

### [x] Web portal phase badges + cost tracking
- Live Research / Dev / Review badges
- Per-project conda env status display
- Real-time token and cost tracking dashboard

## Integration & Ecosystem

### [ ] Integrate claude-scientific-skills
- Repo: https://github.com/K-Dense-AI/claude-scientific-skills
- 170+ domain skills (bioinformatics, chemistry, geospatial, finance, quantum, etc.)
- Zero-code integration: copy skills to `~/.claude/skills/`, idea2paper agents auto-discover
- Strategy: don't install all 170+, curate per-domain bundles to avoid token bloat
- Add domain skill recommendation section to idea2paper docs
- Test: verify skills load correctly when agent runs via `claude -p` with `--no-session-persistence`

### [ ] Codex backend — full feature parity
- Basic invocation works (`codex exec`), but not tested end-to-end on real projects
- Missing: deep research context injection (Codex has no equivalent of Gemini Deep Research)
- Missing: compute backend integration verification (Slurm, SkyPilot)
- Need to test permission model (`--dangerously-bypass-approvals-and-sandbox` implications)

### [ ] Gemini backend — full feature parity
- Deep Research integration works, but agent tool availability differs from Claude
- WebSearch/WebFetch may behave differently in Gemini CLI
- Need to verify: does Gemini CLI respect `~/.claude/skills/`? (probably not — skills are Claude Code specific)
- May need a Gemini-native skill injection mechanism

### [ ] Initialize PaperBanana submodule for AI concept figures
- `submodules/PaperBanana` is registered in `.gitmodules` but uninitialized — empty dir gets rsynced to remote VMs, so every concept figure silently falls back to Nano Banana
- Run `git submodule update --init submodules/PaperBanana` locally
- Ensure VM/cluster bootstrap (e.g. `scripts/setup_gke_cluster.sh`, conda env install in `ark/cli.py`) pulls submodules so remote runs get the full 5-agent pipeline (Retriever → Planner → Stylist → Visualizer → Critic)
- Verify `[research]` extras install PaperBanana's runtime deps (aiofiles, json_repair, etc.) — see comment at `ark/cli.py:3349`
- Optional: download `PaperBananaBench` data for reference-retrieval mode (see `compiler.py:1340`)

### [ ] Switch from Gemini CLI to Antigravity CLI
- Replace `gemini` CLI invocations with the Antigravity CLI as the Gemini-family backend
- Audit call sites: `ark/engines/`, deep-research invocations, any agent runner that shells out to `gemini`
- Verify auth model, prompt/streaming API, and tool-use surface match (or shim if not)
- Update install hints and docs (README, AGENTS.md) once migrated

## Cloud & Compute

### [ ] SkyPilot cloud compute — end-to-end verification
- SkyPilot is now the only cloud path (native AWS/GCP/Azure backend removed); the per-user workspace isolation + central launcher SA design is new and needs real-project validation at scale (see `SKYPILOT_PLAN.md`, `docs/SKYPILOT_TEST.md`)
- Need to test: orchestrator + experiment cluster provisioning, GPU instance types, spot vs on-demand
- Need to test: multi-tenant IAM grant flow and cross-project launches into each user's GCP project
- Need to test: cost tracking accuracy and `autostop` teardown after a run completes

### [ ] Edge device & customized environment support
- Current assumption: agents run on a machine with full internet, pip/conda, and GPU access
- Edge scenarios: Jetson, Raspberry Pi, limited-connectivity labs, air-gapped HPC
- Need: environment capability detection (what's available? GPU? internet? package manager?)
- Need: graceful degradation when tools/packages are unavailable
- Need: pre-built conda environment specs or Docker images for reproducibility
- Consider: offline mode where researcher pre-downloads packages and data

### [x] Pending-queue launch ignores the orchestrator backend _(added 2026-07-02, fixed 2026-07-02)_
- **Was:** the queue-drain (`_advance_pending_queue`) and template
  (`_poll_template_links`) paths forced SLURM-if-available-else-local, ignoring a
  project's configured backend — so a `cloud` project parked as `pending` and later
  promoted launched on the control-plane host instead of the user's VM.
- **Fixed:** extracted `orchestrator_launcher_for(project, spec, session, settings)`
  in `routes.py` as the single launch-dispatch point (cloud config-load + fallback +
  backend select). `_try_submit_or_pending`, `_advance_pending_queue`, and
  `_poll_template_links` all route through it, so promotion honours the configured
  backend and the paths can't drift again. Also: `select_launcher` lost its dead
  cloud branch, and unknown backend types now raise instead of silently running
  local. Covered by `tests/unit/test_launch_dispatch.py`.

## Paper Quality

### [ ] Figure visual layout — known issues
- Figures sometimes overflow column width or have clipped labels
- Font sizes in figures may not match venue template body text
- Multi-panel figure alignment can be off (subplot spacing)
- Visualizer agent diagnoses issues but fixes are sometimes superficial (e.g., only adjusting figsize without fixing underlying layout)
- Need: stricter post-compilation visual checks — compare rendered PDF region against template spec
- Consider: pixel-level overlap detection for text/figure collisions

### [x] Citation authenticity & hallucination
- Implemented API-first citation system (`ark/citation.py`)
- LLM never writes BibTeX — all entries fetched from DBLP / CrossRef official APIs
- Search cascade: DBLP → CrossRef → arXiv → Semantic Scholar
- Researcher agent selects papers from API-verified candidate list only
- Per-iteration verification: every review cycle re-verifies `references.bib`
- Dual-source cross-confirmation (DBLP + CrossRef)
- Preprint → published version auto-upgrade
- Unused citation cleanup (removes uncited entries from `.bib`)
- CLI tools: `ark cite-check`, `ark cite-search`, `ark cite-debug`

### [ ] Table formatting
- Tables can overflow column/page width in two-column venues
- `tabular` vs `tabular*` vs `tabulary` selection not always correct
- Need: table width validation in visualizer phase

## Agent Robustness

### [ ] Stagnation detection improvements
- Meta-debugger catches some stagnation patterns but misses others
- Known gap: agent that produces output but makes no meaningful progress (verbose but empty)
- Need: semantic diff of paper between iterations — if delta is trivial, escalate

### [ ] Multi-language paper support
- Currently assumes English-language papers
- Some venues accept other languages (e.g., Chinese CS conferences)
- Low priority but worth noting

## Developer Experience

### [ ] Test coverage gaps
- 115 tests exist but mostly unit-level
- No integration test that runs a mini pipeline end-to-end
- Need: a small synthetic project that runs plan → experiment → write → review in < 5 min

### [ ] Config validation
- `config.yaml` errors (typos, missing fields) sometimes cause cryptic failures deep in pipeline
- Need: upfront schema validation with clear error messages at startup
