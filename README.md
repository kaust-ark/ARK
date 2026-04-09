<p align="center">
  <strong>English</strong> &bull; <a href="README_zh.md">中文</a> &bull; <a href="README_ar.md">العربية</a>
</p>

<p align="center">
  <img src="https://kaust-ark.github.io/assets/logo_ark_transparent.png" alt="ARK" width="260">
</p>

<h1 align="center">ARK &mdash; Automatic Research Kit</h1>

<p align="center">
  <em>Offload the labour. Steer the science.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="Apache 2.0">
  <a href="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml"><img src="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/agents-8-orange.svg" alt="8 Agents">
  <img src="https://img.shields.io/badge/venues-11+-purple.svg" alt="11+ Venues">
  <img src="https://img.shields.io/badge/tests-106-brightgreen.svg" alt="106 Tests">
</p>

<p align="center">
  <a href="https://kaust-ark.github.io/"><strong>Website</strong></a> &bull;
  <a href="#quick-start">Quick Start</a> &bull;
  <a href="#ark-pipeline">Pipeline</a> &bull;
  <a href="#ark-agents">Agents</a> &bull;
  <a href="#cli-reference">CLI</a>
</p>

---

ARK orchestrates **8 specialized AI agents** to turn a research idea into a paper &mdash; literature search, Slurm experiments, LaTeX drafting, figure generation, and iterative peer review &mdash; while you stay in control via **CLI**, **Web Portal**, or **Telegram**.

```
Give it an idea and a venue. ARK handles the rest.
```

## Papers Written by ARK

<p align="center">
<img src="https://kaust-ark.github.io/assets/paper-example.png" alt="MMA Paper" width="480">
<br>
<a href="https://github.com/JihaoXin/mma"><em>CPU Matrix Multiplication: From Naive to Efficient</em></a>
<br>
<sub>NeurIPS format &bull; 6 pages &bull; 14 iterations</sub>
</p>

---

## ARK Pipeline

ARK runs three phases in sequence. The Review phase loops until the paper reaches the target score.

<p align="center">
  <img src="https://kaust-ark.github.io/assets/pipeline_overview.png" alt="ARK Pipeline" width="700">
</p>

| Phase | What Happens |
|:------|:-------------|
| **Research** | Gemini Deep Research runs a literature survey and gathers background knowledge |
| **Dev** | Iterative experiment cycle: plan &rarr; run on Slurm &rarr; analyze &rarr; write initial draft |
| **Review** | Compile &rarr; Review &rarr; Plan &rarr; Execute &rarr; Validate, repeating until score &ge; threshold |

### Review Loop

Each iteration of the Review phase runs **5 steps**:

<p align="center">
  <img src="https://kaust-ark.github.io/assets/review_loop.png" alt="Review Loop" width="700">
</p>

| Step | Description |
|:-----|:------------|
| **Compile** | LaTeX &rarr; PDF, page count, page images |
| **Review** | AI reviewer scores 1&ndash;10, lists Major &amp; Minor issues |
| **Plan** | Planner creates a prioritized action plan |
| **Execute** | Researcher + Experimenter run in parallel; Writer revises LaTeX |
| **Validate** | Verify changes compile; recompile PDF |

The loop repeats until the score reaches the acceptance threshold &mdash; or you intervene via Telegram.

---

## ARK Agents

<p align="center">
  <img src="https://kaust-ark.github.io/assets/architecture_overview.png" alt="ARK Architecture" width="600">
</p>

| Agent | Role |
|:------|:-----|
| **Reviewer** | Scores the paper against venue standards, generates improvement tasks |
| **Planner** | Turns review feedback into a prioritized action plan |
| **Writer** | Drafts and refines LaTeX sections with DBLP-verified references |
| **Experimenter** | Designs experiments, submits Slurm jobs, analyzes results |
| **Researcher** | Deep literature survey via academic APIs (DBLP, CrossRef, Semantic Scholar) |
| **Visualizer** | Generates figures with Nano Banana and venue-aware canvas geometry |
| **Meta-Debugger** | Detects stalls, diagnoses failures, triggers self-repair |
| **Coder** | Writes and debugs experiment code and analysis scripts |

---

## What Sets ARK Apart

| | Other Tools | ARK |
|---|:------------|:----|
| **Control** | Fully autonomous &mdash; drifts from intent, no mid-run correction | Human-in-the-loop: pause at key decisions, steer via Telegram or web |
| **Formatting** | Broken layouts, LaTeX errors, manual cleanup | Hard-coded LaTeX + venue templates (NeurIPS, ACL, IEEE&hellip;) |
| **Citations** | LLMs fabricate plausible-looking references | Every citation verified against DBLP &mdash; no fake references |
| **Figures** | Default styles, wrong sizes, no page awareness | Nano Banana + venue-aware canvas, column widths, and fonts |

---

## Quick Start

```bash
# Install
pip install -e .

# Create a project (interactive wizard)
ark new mma

# Run — ARK takes it from here
ark run mma

# Monitor in real time
ark monitor mma

# Check progress
ark status mma
```

The wizard walks you through: code directory, venue, research idea, authors, compute backend, figure generation, and Telegram setup.

### Start from an Existing PDF

```bash
ark new mma --from-pdf proposal.pdf
```

ARK parses the PDF with PyMuPDF + Claude Haiku, pre-fills the wizard, and kicks off from the extracted spec.

---

## CLI Reference

| Command | Description |
|:--------|:------------|
| `ark new <name>` | Create project via interactive wizard |
| `ark run <name>` | Launch the autonomous pipeline |
| `ark status [name]` | Score, iteration, phase, cost |
| `ark monitor <name>` | Live dashboard: agent activity, score trend |
| `ark update <name>` | Inject a mid-run instruction |
| `ark stop <name>` | Gracefully stop |
| `ark restart <name>` | Stop + restart |
| `ark research <name>` | Run Gemini Deep Research standalone |
| `ark config <name> [key] [val]` | View or edit config |
| `ark clear <name>` | Reset state for a fresh start |
| `ark delete <name>` | Remove project entirely |
| `ark setup-bot` | Configure Telegram bot |
| `ark list` | List all projects with status |
| `ark webapp install` | Install web portal service |

---

## Web Portal

ARK includes a web-based portal for managing projects, viewing scores, and steering agents.

### Configuration

The web app is configured via `webapp.env` located in your ARK config directory (default: `.ark/webapp.env` in the project root). This file is created automatically on the first run of `ark webapp`.

#### Authentication & Access
- **SMTP**: Required for "Magic Link" login. Set `SMTP_HOST`, `SMTP_USER`, and `SMTP_PASSWORD`.
- **Restrictions**: Use `ALLOWED_EMAILS` (specific users) or `EMAIL_DOMAINS` (entire organizations) to limit access.
- **Google OAuth**: Optional. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

### Management Commands

| Command | Description |
|:--------|:------------|
| `ark webapp` | Start the app in the foreground (useful for debugging). |
| `ark webapp release` | Tag the current code and deploy to the production worktree. |
| `ark webapp install [--dev]` | Install and start as a `systemd` user service. |
| `ark webapp status` | Show status of the systemd service. |
| `ark webapp restart` | Restart the webapp service. |
| `ark webapp logs [-f]` | View or tail service logs. |

<details>
<summary><strong>Service Details (Prod vs. Dev)</strong></summary>

| | Prod | Dev |
|--|:-----|:----|
| **Port** | 9527 | 1027 |
| **Service Name** | `ark-webapp` | `ark-webapp-dev` |
| **Conda Env** | `ark-prod` | `ark-dev` |
| **Code Source** | `~/.ark/prod/` (pinned) | Current repository (live) |

</details>


<details>
<summary><strong>Direct orchestrator invocation</strong></summary>

```bash
python -m ark.orchestrator --project mma --mode paper --max-iterations 20
python -m ark.orchestrator --project mma --mode dev
```

</details>

---

## Telegram Integration

```bash
ark setup-bot    # one-time: paste BotFather token, auto-detect chat ID
```

What you get:
- **Live notifications** &mdash; score changes, phase transitions, errors
- **Send instructions** &mdash; steer the current iteration
- **Request PDFs** &mdash; latest compiled paper sent to chat
- **Proactive confirmations** &mdash; ARK asks before key decisions

---

## Requirements

- **Python 3.9+** with `pyyaml` and `PyMuPDF`
- [**Claude Code**](https://docs.anthropic.com/en/docs/claude-code) CLI installed and authenticated
- **Claude Max subscription recommended** &mdash; very token-intensive
- Optional: LaTeX (`pdflatex` + `bibtex`), Slurm, `google-genai` for AI figures

```bash
pip install -e .                    # Core
pip install -e ".[research]"       # + Gemini Deep Research & Nano Banana
```

## Supported Venues

NeurIPS &bull; ICML &bull; ICLR &bull; AAAI &bull; ACL &bull; IEEE &bull; ACM SIGPLAN &bull; ACM SIGCONF &bull; LNCS &bull; MLSys &bull; USENIX &mdash; plus aliases for PLDI, ASPLOS, SOSP, EuroSys, OSDI, NSDI, INFOCOM, and more.

## License

[Apache 2.0](LICENSE)

<p align="center">
  <sub>Built by <a href="https://sands.kaust.edu.sa/">SANDS Lab, KAUST</a></sub>
</p>
