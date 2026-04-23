"""Project-completion summary.

Given a finished project directory, produce a 1-2 page markdown summary
comparing what the user's original idea promised against what actually
landed in the paper, what the reviewer still complains about, and a
short list of next-step recommendations. Rendered to PDF and attached
to the completion email alongside the paper itself.

Separate from the orchestrator's agent pipeline: a single `claude -p`
call, ~60-90 seconds, fails soft (the existing completion email still
goes out if summary generation has trouble).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("ark.summary")


_PROMPT_TEMPLATE = """\
You are writing a 1-2 page project-completion summary for the author of
a research paper that was just produced by an automated pipeline. The
author wants a crisp, honest accounting of what happened vs. what they
asked for — not marketing copy.

## Inputs (read these files, do not guess)

- Original idea (what the user asked for):
{idea_md}

- Latest review (the reviewer's final verdict on the produced paper):
{review_md}

- Findings from the experiment runs (status per protocol item,
  deferrals, library use):
{findings_yaml}

- Score trajectory across iterations:
{paper_state_yaml}

- Human-intervention escalations (if any were raised during the run):
{needs_human_json}

## Output format (markdown, exactly these sections, in this order)

# Project Summary: {project_title}

**Final score**: X/10 after N iterations.
**Venue**: {venue_name}

## What the idea asked for

2-3 sentences summarizing the original idea in the author's own voice.
Not a rewrite — capture the concrete deliverables they named.

## What landed

Bullet list. Each bullet is a concrete deliverable the paper now
contains, with a reference to where (section / figure / table number if
knowable). Only list things actually in the final paper — no
aspirational claims.

## What didn't land

Bullet list of things the original idea named or the experimental
protocol required, that did NOT make it into the final paper. Each
bullet must include the *reason* (compute unavailable, tool missing,
deferred, out of scope, time constraint), not just the omission. If
there are no gaps, say so in one line — do not invent gaps.

## Unresolved reviewer concerns

Bullet list of Major + notable Minor issues from the latest review
that remain unaddressed. Use the reviewer's own framing. Mark each
with M/m and the issue ID if present. If all issues are resolved,
say so.

## Recommended next steps

3-5 bullets, each actionable and specific:
- What to do next
- Why (tied to a specific gap or reviewer concern above)
- Estimated effort (hours / days / blocked-on-resource)

Order by leverage — highest-impact-for-lowest-effort first.

## Honesty notes

One short paragraph flagging anything a reader should know about the
artifact's trust level: deferred baselines, fabrication risk, sanity
anomalies, etc. If nothing to flag, omit this section entirely.

---

Hard rules:
- Do NOT invent content not supported by the inputs.
- Do NOT claim work was done that the findings don't support.
- Keep total length under ~700 words.
- Use plain markdown, no HTML.
- Write directly in the summary style — no preamble like "Here is the
  summary" or "Below is your report". Start with the H1.
"""


def _read_optional(path: Path, max_chars: int = 20000) -> str:
    """Read a file, tolerating missing/unreadable ones with a placeholder."""
    try:
        if not path.exists():
            return "(file not present)"
        data = path.read_text(errors="replace")
        if len(data) > max_chars:
            data = data[:max_chars] + "\n... [truncated] ..."
        return data
    except Exception as e:
        return f"(could not read {path.name}: {e})"


def _infer_project_title(project_dir: Path, config: dict) -> str:
    """Best-effort project title: config.title > first H1 in idea > slug."""
    title = (config.get("title") or "").strip()
    if title:
        return title
    idea = project_dir / "auto_research" / "state" / "idea.md"
    if idea.exists():
        for line in idea.read_text(errors="replace").splitlines()[:20]:
            if line.startswith("# "):
                return line[2:].strip()
    return config.get("project") or project_dir.name


def generate_project_summary(
    project_dir: Path,
    claude_cmd: str = "claude",
    model: str = "claude-sonnet-4-6",
    timeout: int = 240,
) -> str:
    """Run one `claude -p` call against the project's artifacts and return
    a markdown summary. Returns the summary string on success, raises on
    failure — callers should wrap in try/except and fall back to sending
    the paper PDF alone.
    """
    project_dir = Path(project_dir)
    state = project_dir / "auto_research" / "state"
    results = project_dir / "results"

    config_path = project_dir / "config.yaml"
    config = {}
    if config_path.exists():
        try:
            config = yaml.safe_load(config_path.read_text()) or {}
        except Exception:
            config = {}

    prompt = _PROMPT_TEMPLATE.format(
        idea_md=_read_optional(state / "idea.md"),
        review_md=_read_optional(state / "latest_review.md"),
        findings_yaml=_read_optional(state / "findings.yaml", max_chars=10000),
        paper_state_yaml=_read_optional(state / "paper_state.yaml", max_chars=5000),
        needs_human_json=_read_optional(results / "needs_human.json", max_chars=3000),
        project_title=_infer_project_title(project_dir, config),
        venue_name=config.get("venue") or config.get("venue_format") or "Unknown",
    )

    cmd = [
        claude_cmd, "-p", prompt,
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--output-format", "json",
        "--model", model,
    ]
    env = os.environ.copy()
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude -p exited {proc.returncode}: {proc.stderr[:500]}"
        )
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"claude output was not JSON: {e}; raw: {proc.stdout[:500]}")
    md = parsed.get("result") or parsed.get("text") or ""
    if not md.strip():
        raise RuntimeError(f"claude returned empty result: {parsed!r}")
    return md


_PDF_CSS = """
@page {
    size: A4;
    margin: 2.2cm 2cm;
    @bottom-right {
        content: counter(page) " / " counter(pages);
        font-family: "Helvetica", "Arial", sans-serif;
        font-size: 9pt;
        color: #777;
    }
}
body {
    font-family: "Helvetica Neue", "Helvetica", "Arial", sans-serif;
    font-size: 10.5pt;
    line-height: 1.45;
    color: #222;
}
h1 { font-size: 20pt; margin: 0 0 .2em 0; color: #111; }
h1 + p, h1 + p + p { margin: .15em 0; color: #444; font-size: 10.5pt; }
h2 { font-size: 13pt; margin: 1.2em 0 .3em 0; color: #1a4e8a; border-bottom: 1px solid #ddd; padding-bottom: 2px; }
h3 { font-size: 11.5pt; margin: .9em 0 .2em 0; color: #333; }
p  { margin: .35em 0; }
ul, ol { margin: .3em 0 .3em 1.3em; padding: 0; }
li { margin-bottom: .18em; }
code { background: #f4f4f4; padding: 1px 4px; border-radius: 3px; font-size: 9.5pt; }
pre { background: #f4f4f4; padding: 8px 10px; border-radius: 4px; font-size: 9pt; overflow: hidden; }
hr { border: none; border-top: 1px solid #ccc; margin: 1em 0; }
strong { color: #111; }
a { color: #1a4e8a; text-decoration: none; }
"""


def render_markdown_to_pdf(md: str, out_path: Path) -> Path:
    """Convert a markdown string to PDF at out_path. Returns out_path.

    Uses python-markdown for md→html and weasyprint for html→pdf. Both
    are already installed in the project environment.
    """
    import markdown
    from weasyprint import HTML, CSS

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    html_body = markdown.markdown(
        md, extensions=["extra", "sane_lists"]
    )
    html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
        f"<body>{html_body}</body></html>"
    )
    HTML(string=html).write_pdf(
        str(out_path), stylesheets=[CSS(string=_PDF_CSS)]
    )
    return out_path


def generate_summary_pdf(
    project_dir: Path,
    out_path: Optional[Path] = None,
    claude_cmd: str = "claude",
    model: str = "claude-sonnet-4-6",
    timeout: int = 240,
) -> Path:
    """One-shot helper: generate markdown, render to PDF, return the
    PDF path. Default output is ``<project_dir>/summary.pdf``."""
    project_dir = Path(project_dir)
    if out_path is None:
        out_path = project_dir / "summary.pdf"
    md = generate_project_summary(
        project_dir, claude_cmd=claude_cmd, model=model, timeout=timeout,
    )
    # Also save the markdown next to the pdf for debugging / resend.
    (out_path.with_suffix(".md")).write_text(md)
    return render_markdown_to_pdf(md, out_path)
