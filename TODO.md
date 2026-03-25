# ARK TODO & Known Issues

## Integration & Ecosystem

### [ ] Integrate claude-scientific-skills
- Repo: https://github.com/K-Dense-AI/claude-scientific-skills
- 170+ domain skills (bioinformatics, chemistry, geospatial, finance, quantum, etc.)
- Zero-code integration: copy skills to `~/.claude/skills/`, ARK agents auto-discover
- Strategy: don't install all 170+, curate per-domain bundles to avoid token bloat
- Add domain skill recommendation section to ARK docs
- Test: verify skills load correctly when agent runs via `claude -p` with `--no-session-persistence`

### [ ] Codex backend — full feature parity
- Basic invocation works (`codex exec`), but not tested end-to-end on real projects
- Missing: deep research context injection (Codex has no equivalent of Gemini Deep Research)
- Missing: compute backend integration verification (Slurm, cloud)
- Need to test permission model (`--dangerously-bypass-approvals-and-sandbox` implications)

### [ ] Gemini backend — full feature parity
- Deep Research integration works, but agent tool availability differs from Claude
- WebSearch/WebFetch may behave differently in Gemini CLI
- Need to verify: does Gemini CLI respect `~/.claude/skills/`? (probably not — skills are Claude Code specific)
- May need a Gemini-native skill injection mechanism

## Cloud & Compute

### [ ] AWS cloud compute — end-to-end verification
- Compute backend code exists (EC2 provisioning, rsync, SSH execution) but never validated on real AWS
- Need to test: instance provisioning, security group setup, GPU instance types, spot vs on-demand
- Need to test: cost tracking accuracy for cloud compute hours
- Need to test: cleanup/termination after experiment completes

### [ ] GCP / Azure cloud compute — verification
- Same as AWS — code exists, untested in production
- GCP: verify gcloud CLI integration, GPU quota handling
- Azure: verify az CLI integration, VM provisioning

### [ ] Edge device & customized environment support
- Current assumption: agents run on a machine with full internet, pip/conda, and GPU access
- Edge scenarios: Jetson, Raspberry Pi, limited-connectivity labs, air-gapped HPC
- Need: environment capability detection (what's available? GPU? internet? package manager?)
- Need: graceful degradation when tools/packages are unavailable
- Need: pre-built conda environment specs or Docker images for reproducibility
- Consider: offline mode where researcher pre-downloads packages and data

## Paper Quality

### [ ] Figure visual layout — known issues
- Figures sometimes overflow column width or have clipped labels
- Font sizes in figures may not match venue template body text
- Multi-panel figure alignment can be off (subplot spacing)
- Visualizer agent diagnoses issues but fixes are sometimes superficial (e.g., only adjusting figsize without fixing underlying layout)
- Need: stricter post-compilation visual checks — compare rendered PDF region against template spec
- Consider: pixel-level overlap detection for text/figure collisions

### [ ] Citation authenticity & hallucination
- LLM-generated references are frequently hallucinated (wrong author, wrong year, non-existent papers)
- Current pipeline has no citation verification step
- Need: post-write citation verification phase
  - Cross-check each `\cite{}` entry against Semantic Scholar / CrossRef / Google Scholar API
  - Verify: title exists, authors match, year matches, DOI resolves
  - Flag or remove unverifiable citations
- Need: researcher agent should provide real BibTeX entries from actual database queries, not LLM memory
- Consider: mandatory `references.bib` sourced exclusively from API-fetched entries

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
- 46 tests exist but mostly unit-level
- No integration test that runs a mini pipeline end-to-end
- Need: a small synthetic project that runs plan → experiment → write → review in < 5 min

### [ ] Config validation
- `config.yaml` errors (typos, missing fields) sometimes cause cryptic failures deep in pipeline
- Need: upfront schema validation with clear error messages at startup
