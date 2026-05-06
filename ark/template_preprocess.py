"""Preprocess user-uploaded LaTeX templates before the pipeline touches them.

When a user uploads a custom template ZIP (e.g. the NeurIPS sample), the
extracted ``main.tex`` typically ships with:

* The venue's own placeholder title / author / abstract (e.g.
  ``\\title{Formatting Instructions For NeurIPS 2026}``)
* Hundreds of lines of author-facing instructions between ``\\maketitle`` and
  ``\\end{document}`` that the template expects the author to delete.

Without preprocessing, the writer/reviewer agents treat that instructional
prose as already-written paper content and won't replace it, which is how
the user's d9b7fab8 project ended up compiling to a 12-page NeurIPS
instructions document with the research content shoehorned into two pages.

This module normalises the upload so that downstream agents see a clean
skeleton:

1. ``sanitize_tex_metadata``: empty the title / author / abstract placeholders.
   Pure regex, no LLM — must never fail silently on well-formed LaTeX.
2. ``detect_boilerplate_span``: find the region between ``\\maketitle`` and
   the bibliography / appendix that holds template instructions, using a
   structural heuristic (works reliably for NeurIPS / ICML / ACL style
   templates).  Not LLM-based — keeps the upload path deterministic and
   offline-safe.
3. ``stub_out_boilerplate``: replace that region with an empty section
   skeleton the writer is meant to fill.
4. ``write_template_manifest``: emit ``paper/template_manifest.yaml`` so the
   writer prompt (rendered by ``_sync_paper_metadata``) can remind the agent
   about preserved files, page limits, and the sections it is expected to
   populate.

The whole pipeline degrades gracefully: if the heuristic can't locate a
boilerplate region, we still write a manifest noting the sanitisation that
happened, and the pipeline proceeds with an otherwise-untouched template.
"""
from __future__ import annotations

import re
import yaml
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
#  LaTeX command helpers
# ---------------------------------------------------------------------------

def _active_command_regex(cmd: str) -> re.Pattern[str]:
    """Match an active ``\\<cmd>`` at the start of a line (not inside a
    comment), positioned right before the opening ``{``.  ``^\\s*`` via
    MULTILINE ensures commented-out occurrences (lines starting with ``%``)
    are skipped.
    """
    return re.compile(rf'^(?P<indent>[ \t]*)\\{cmd}\s*(?=\{{)', re.MULTILINE)


def _find_matching_brace(src: str, open_pos: int) -> Optional[int]:
    """Given ``src[open_pos] == '{'``, walk balanced braces and return the
    index of the matching ``}``.  Returns ``None`` if unbalanced.  Honours
    LaTeX escapes (``\\{`` / ``\\}``).
    """
    if open_pos >= len(src) or src[open_pos] != '{':
        return None
    depth = 1
    i = open_pos + 1
    while i < len(src) and depth > 0:
        ch = src[i]
        if ch == '\\' and i + 1 < len(src):
            i += 2
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
        i += 1
    return i - 1 if depth == 0 else None


def _clear_command_body(src: str, cmd: str, placeholder: str = "") -> tuple[str, Optional[str]]:
    """Replace the body of the first active ``\\<cmd>{...}`` with ``placeholder``.

    Returns ``(new_src, original_body)`` where ``original_body`` is the text
    that was replaced (or ``None`` if the command wasn't found).
    """
    for m in _active_command_regex(cmd).finditer(src):
        brace_start = m.end()
        close = _find_matching_brace(src, brace_start)
        if close is None:
            continue
        original = src[brace_start + 1:close]
        new_src = src[:brace_start + 1] + placeholder + src[close:]
        return new_src, original
    return src, None


def _clear_abstract_env(src: str, placeholder: str) -> tuple[str, Optional[str]]:
    """Empty the first ``\\begin{abstract}...\\end{abstract}`` body."""
    m = re.search(r'\\begin\{abstract\}(.*?)\\end\{abstract\}', src, re.DOTALL)
    if not m:
        return src, None
    original = m.group(1).strip()
    replacement = f"\\begin{{abstract}}\n{placeholder}\n\\end{{abstract}}"
    return src[:m.start()] + replacement + src[m.end():], original


# ---------------------------------------------------------------------------
#  Step 1 — metadata sanitisation
# ---------------------------------------------------------------------------

# Non-empty placeholder for \title so the downstream ``_sync_paper_metadata``
# (which requires an active ``\title{...}`` to match) can still find and
# rewrite it.  The sentinel MUST be LaTeX-safe — underscores and other
# special chars would break a validation compile that runs before
# ``_sync_paper_metadata`` fills in the real title.
_TITLE_SENTINEL = "ARK Pending Title"
_ABSTRACT_SENTINEL = "% [ABSTRACT WILL BE FILLED BY WRITER]"


def sanitize_tex_metadata(src: str) -> tuple[str, dict]:
    """Clear out the venue's placeholder title / author / abstract.

    Parameters
    ----------
    src
        Contents of ``main.tex`` immediately after extraction.

    Returns
    -------
    new_src
        ``main.tex`` contents with empty metadata slots ready to be filled
        by ``_sync_paper_metadata`` and the writer agent.
    detected
        ``{"title_placeholder": ..., "author_placeholder": ...,
            "abstract_placeholder": ...}`` — useful for the manifest and
        for logging what the template originally carried.  Truncated to
        200 chars per field to keep the manifest small.
    """
    detected: dict = {}

    new_src, title_was = _clear_command_body(src, "title", placeholder=_TITLE_SENTINEL)
    if title_was is not None:
        detected["title_placeholder"] = title_was.strip()[:200]
        src = new_src

    new_src, author_was = _clear_command_body(src, "author", placeholder="")
    if author_was is not None:
        # Author blocks can be long (authors + affiliations + emails).  Keep
        # just the first meaningful line for the manifest — the NeurIPS
        # template opens with ``\author{%`` so ``first non-empty line`` would
        # pick the ``%`` comment.  Filter those out.
        excerpt = ""
        for line in author_was.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("%"):
                continue
            excerpt = stripped
            break
        detected["author_placeholder"] = excerpt[:200]
        src = new_src

    new_src, abs_was = _clear_abstract_env(src, placeholder=_ABSTRACT_SENTINEL)
    if abs_was:
        detected["abstract_placeholder"] = abs_was[:200]
        src = new_src

    return src, detected


# ---------------------------------------------------------------------------
#  Step 2 — boilerplate span detection
# ---------------------------------------------------------------------------

# Structural markers used to bound the boilerplate region.  Everything
# between ``\maketitle`` (or ``\end{abstract}``, whichever comes later) and
# the first bibliography / acknowledgements / appendix marker is assumed to
# be template instructions the author was meant to replace.
_BODY_START_MARKERS = (r"\end{abstract}", r"\maketitle")
_BODY_END_MARKERS = (
    r"\begin{ack}",
    r"\section*{References}",
    r"\section*{Acknowledgments}",
    r"\section*{Acknowledgements}",
    r"\bibliography{",
    r"\printbibliography",
    r"\appendix",
)


def detect_boilerplate_span(src: str) -> Optional[tuple[int, int]]:
    """Return ``(start, end)`` character offsets of the boilerplate body,
    or ``None`` if markers aren't found.

    Span boundaries:

    * ``start`` — immediately after the later of ``\\end{abstract}`` or
      ``\\maketitle``.
    * ``end`` — immediately before the first end-marker found after
      ``start`` (references, acknowledgements, appendix, bibliography).

    Callers can use the span to replace that region with a clean skeleton.
    """
    start = -1
    for marker in _BODY_START_MARKERS:
        idx = src.find(marker)
        if idx < 0:
            continue
        after = idx + len(marker)
        if after > start:
            start = after
    if start < 0:
        return None

    end = -1
    for marker in _BODY_END_MARKERS:
        idx = src.find(marker, start)
        if idx < 0:
            continue
        if end < 0 or idx < end:
            end = idx
    if end < 0:
        return None

    return start, end


# Appendix-region end-markers.  ``\input{checklist.tex}`` is the NeurIPS
# mandatory self-review section and must survive intact.  ``\end{document}``
# is the last resort when the template has no checklist.
_APPENDIX_END_MARKERS = (
    r"\input{",
    r"\include{",
    r"\end{document}",
)


def detect_appendix_boilerplate_span(src: str) -> Optional[tuple[int, int]]:
    """Return the offsets of the instruction prose that typically follows
    ``\\appendix`` in venue templates (e.g. NeurIPS's "Technical appendices
    and supplementary material" section with its "optional reading" blurb).

    Span boundaries:

    * ``start`` — immediately after ``\\appendix``.
    * ``end``   — immediately before the first ``\\input{...}`` /
      ``\\include{...}`` (checklist, supplementary) or ``\\end{document}``.

    Returns ``None`` if the template has no ``\\appendix`` or no end-marker
    after it.
    """
    marker = r"\appendix"
    idx = src.find(marker)
    if idx < 0:
        return None
    start = idx + len(marker)

    end = -1
    for em in _APPENDIX_END_MARKERS:
        i = src.find(em, start)
        if i < 0:
            continue
        if end < 0 or i < end:
            end = i
    if end < 0:
        return None

    return start, end


# ---------------------------------------------------------------------------
#  Step 3 — boilerplate replacement
# ---------------------------------------------------------------------------

_WRITER_SCAFFOLD = """

% [ARK: template instructions removed by preprocess_custom_template]
% The writer agent is expected to populate the sections below with the
% research content described in auto_research/state/idea.md.

\\section{Introduction}
% TO BE WRITTEN

\\section{Related Work}
% TO BE WRITTEN

\\section{Method}
% TO BE WRITTEN

\\section{Experiments}
% TO BE WRITTEN

\\section{Discussion}
% TO BE WRITTEN

\\section{Conclusion}
% TO BE WRITTEN

\\section*{LLM Usage Statement}
This paper was produced with the assistance of ARK (idea2paper.org), an autonomous research framework powered by large language models. The author(s) should review all content and assume ultimate responsibility for its correctness, originality, and integrity.

"""


def stub_out_boilerplate(src: str, span: tuple[int, int]) -> str:
    """Replace ``src[span[0]:span[1]]`` with an empty section skeleton."""
    start, end = span
    return src[:start] + _WRITER_SCAFFOLD + src[end:]


_APPENDIX_SCAFFOLD = """

% [ARK: template appendix instructions removed by preprocess_custom_template]
% Populate this region with supplementary material (full proofs, extended
% ablations, hyperparameter sweeps, etc.) only when it genuinely belongs in
% an appendix.  Leave this region minimal if you have no such content.

"""


def stub_out_appendix_boilerplate(src: str, span: tuple[int, int]) -> str:
    """Replace the template's appendix instruction prose (everything between
    ``\\appendix`` and the first ``\\input``/``\\end{document}``) with a
    small scaffold the writer can expand or leave near-empty.
    """
    start, end = span
    return src[:start] + _APPENDIX_SCAFFOLD + src[end:]


# ---------------------------------------------------------------------------
#  Step 4 — manifest
# ---------------------------------------------------------------------------

def _detect_constraints(paper_dir: Path, src: str) -> dict:
    """Best-effort extraction of venue-specific constraints from the upload.

    Looks at the style files present in ``paper_dir`` and at known page-limit
    strings in the tex body.  All fields optional; ``{}`` is a valid result.
    """
    constraints: dict = {}

    # Must-preserve files: any .sty / .cls / .bst the user shipped.  Also
    # preserve any file that's \input-ed from main.tex (e.g. checklist.tex).
    must_preserve = []
    for f in sorted(paper_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in (".sty", ".cls", ".bst"):
            must_preserve.append(f.name)
    for m in re.finditer(r'\\input\{([^}]+)\}', src):
        ref = m.group(1).strip()
        # Keep the raw reference (with or without .tex) so the writer knows
        # not to delete the file.
        if ref not in must_preserve:
            must_preserve.append(ref)
    if must_preserve:
        constraints["must_preserve_files"] = must_preserve

    # Page limits — very venue-specific; match a couple of common phrasings.
    # NeurIPS: "Papers may only be up to {\bf nine} pages long".
    # ICML: "must not exceed 8 pages".
    page_patterns = [
        (r"up to\s*\{?\\?\w*?\s*(\w+)\}?\s+pages", 1),
        (r"(?:must not exceed|limited to|limit of)\s+(\w+)\s+pages", 1),
    ]
    word_to_int = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
        "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    }
    for pat, grp in page_patterns:
        m = re.search(pat, src, re.IGNORECASE)
        if not m:
            continue
        raw = m.group(grp).strip().lower()
        if raw.isdigit():
            constraints["page_limit"] = int(raw)
            break
        if raw in word_to_int:
            constraints["page_limit"] = word_to_int[raw]
            break

    return constraints


_DEFAULT_SECTIONS = [
    "Introduction",
    "Related Work",
    "Method",
    "Experiments",
    "Discussion",
    "Conclusion",
]


# Map .sty / .cls filename prefixes to a venue name known by
# ``ark.latex_geometry.get_geometry``. Order matters: first match wins, so
# put more specific patterns first.
_VENUE_FILE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^neurips", re.IGNORECASE), "neurips"),
    (re.compile(r"^iclr", re.IGNORECASE), "iclr"),
    (re.compile(r"^icml", re.IGNORECASE), "icml"),
    (re.compile(r"^(?:acl|emnlp|naacl)", re.IGNORECASE), "acl"),
    (re.compile(r"^IEEE", re.IGNORECASE), "ieee"),
    (re.compile(r"^acmart", re.IGNORECASE), "acmart-sigplan"),
    (re.compile(r"^sigplan", re.IGNORECASE), "sigplan"),
    (re.compile(r"^llncs|^lncs", re.IGNORECASE), "lncs"),
    (re.compile(r"^usenix", re.IGNORECASE), "usenix"),
)


def detect_venue_format(paper_dir: Path) -> Optional[str]:
    """Detect a known venue name by inspecting ``.sty`` / ``.cls`` filenames.

    Returns the venue key (e.g. ``"neurips"``, ``"icml"``) suitable for
    passing to ``ark.latex_geometry.get_geometry``, or ``None`` if no
    pattern matched. Pure filename inspection — never reads file contents.
    """
    for ext in (".sty", ".cls"):
        for path in sorted(paper_dir.glob(f"*{ext}")):
            for pattern, venue in _VENUE_FILE_PATTERNS:
                if pattern.match(path.name):
                    return venue
    return None


def _extract_section_headers(tex: str) -> list[str]:
    """Return the ``\\section{...}`` and ``\\section*{...}`` titles in ``tex``.

    Used to tell the writer which headers were present in the removed
    boilerplate so it knows not to re-introduce the template's own sections
    (e.g. "Submission of papers to NeurIPS 2026").
    """
    headers: list[str] = []
    for m in re.finditer(r'\\section\*?\{([^}]+)\}', tex):
        headers.append(m.group(1).strip())
    return headers


def build_manifest(
    original_src: str,
    sanitized_src: str,
    detected_placeholders: dict,
    boilerplate_span: Optional[tuple[int, int]],
    paper_dir: Path,
    venue_hint: str = "",
    removed_headers: Optional[list[str]] = None,
    appendix_removed_chars: int = 0,
) -> dict:
    """Assemble the template_manifest.yaml payload.

    ``removed_headers`` — section titles captured from the boilerplate region
    *before* it was stubbed out.  Pass it explicitly from the orchestrator;
    extracting from ``sanitized_src`` after stubbing would surface the writer
    skeleton's own headers instead.

    ``appendix_removed_chars`` — number of characters stripped between
    ``\\appendix`` and the first ``\\input``/``\\end{document}``.  0 when
    the template has no appendix boilerplate.
    """
    manifest: dict = {
        "source": "user_uploaded_zip",
        "venue_hint": venue_hint or "unknown",
        "detected_venue_format": detect_venue_format(paper_dir),
        "detected_placeholders": detected_placeholders,
        "constraints": _detect_constraints(paper_dir, original_src),
        "writer_instructions": {
            "sections_to_fill": _DEFAULT_SECTIONS,
            "do_not_reintroduce_template_instructions": True,
        },
    }
    if boilerplate_span is not None:
        start, end = boilerplate_span
        manifest["boilerplate_removed"] = {
            "chars": end - start,
            "removed_section_headers": removed_headers or [],
        }
    else:
        manifest["boilerplate_removed"] = None
        manifest["writer_instructions"]["warning"] = (
            "Structural markers not found — boilerplate was NOT removed. "
            "The writer must manually delete any template instruction prose."
        )
    if appendix_removed_chars > 0:
        manifest["appendix_boilerplate_removed_chars"] = appendix_removed_chars
    return manifest


def write_template_manifest(paper_dir: Path, manifest: dict) -> Path:
    path = paper_dir / "template_manifest.yaml"
    path.write_text(
        yaml.dump(manifest, default_flow_style=False, allow_unicode=True, sort_keys=False)
    )
    return path


# ---------------------------------------------------------------------------
#  Agent-prompt notes block
# ---------------------------------------------------------------------------

def render_custom_template_notes(paper_dir: Path) -> str:
    """Render the ``{CUSTOM_TEMPLATE_NOTES}`` block for writer/reviewer prompts.

    Reads ``paper_dir/template_manifest.yaml`` and summarises it for the
    agent.  Returns the empty string (no newline) when no manifest is
    present — so non-custom projects see nothing extra in their prompts.
    """
    manifest_path = paper_dir / "template_manifest.yaml"
    if not manifest_path.exists():
        return ""
    try:
        manifest = yaml.safe_load(manifest_path.read_text()) or {}
    except Exception:
        return ""

    lines: list[str] = ["", "## Custom Template Notice", ""]
    lines.append(
        "This project uses a user-uploaded template. A preprocessing pass has "
        "already cleared the template's placeholder title / author / abstract "
        "and replaced its instructional body text with empty section stubs."
    )
    lines.append("")

    boilerplate = manifest.get("boilerplate_removed")
    if isinstance(boilerplate, dict):
        removed_headers = boilerplate.get("removed_section_headers") or []
        if removed_headers:
            shown = ", ".join(f'"{h}"' for h in removed_headers[:6])
            if len(removed_headers) > 6:
                shown += f", ... ({len(removed_headers) - 6} more)"
            lines.append(
                f"**Removed template sections** (do NOT re-introduce as paper content): {shown}."
            )
            lines.append("")

    constraints = manifest.get("constraints") or {}
    must_preserve = constraints.get("must_preserve_files") or []
    if must_preserve:
        lines.append("**Must-preserve files** (do not rename or delete):")
        for name in must_preserve:
            lines.append(f"- `{name}`")
        lines.append("")

    page_limit = constraints.get("page_limit")
    if page_limit:
        lines.append(f"**Detected page limit from template**: {page_limit} pages.")
        lines.append("")

    writer_instr = manifest.get("writer_instructions") or {}
    sections = writer_instr.get("sections_to_fill") or []
    if sections:
        lines.append(
            "**Sections to populate** (empty stubs are already in main.tex): "
            + ", ".join(sections)
            + "."
        )
        lines.append("")

    if writer_instr.get("warning"):
        lines.append(f"**⚠ Preprocessing warning**: {writer_instr['warning']}")
        lines.append("")

    lines.append(
        "The sanitized main.tex may still contain template residue (e.g. "
        "example reference entries in the References section, unanswered "
        "questions in the checklist). The `TO BE WRITTEN` markers are your "
        "primary targets, but scan the whole file and treat anything that "
        "looks like placeholder prose as editable."
    )
    lines.append("")
    lines.append(
        "Full preprocessing record: `paper/template_manifest.yaml`. Read it "
        "if you need details about what was detected."
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Top-level entrypoint
# ---------------------------------------------------------------------------

def preprocess_custom_template(paper_dir: Path, venue_hint: str = "") -> dict:
    """Run the full preprocessing pipeline on ``paper_dir/main.tex``.

    Writes the sanitised ``main.tex`` and ``template_manifest.yaml`` in
    place.  Returns the manifest dict for logging / UI display.

    Raises ``FileNotFoundError`` if ``main.tex`` is missing.  All other
    failure modes degrade gracefully (manifest records what succeeded).
    """
    main_tex = paper_dir / "main.tex"
    if not main_tex.exists():
        raise FileNotFoundError(f"main.tex not found in {paper_dir}")

    original_src = main_tex.read_text()

    sanitized_src, detected_placeholders = sanitize_tex_metadata(original_src)

    boilerplate_span = detect_boilerplate_span(sanitized_src)
    removed_headers: list[str] = []
    if boilerplate_span is not None:
        # Capture the original section headers before stubbing them out, so
        # the manifest can tell the writer which template sections disappear.
        removed_headers = _extract_section_headers(
            sanitized_src[boilerplate_span[0]:boilerplate_span[1]]
        )
        sanitized_src = stub_out_boilerplate(sanitized_src, boilerplate_span)

    # Second pass: stub out the template's appendix instruction prose.
    # Must run AFTER the main boilerplate stub so offsets aren't invalidated
    # mid-edit — detect_appendix_boilerplate_span runs on the now-stubbed
    # source and finds \appendix (which is preserved, not touched above).
    appendix_span = detect_appendix_boilerplate_span(sanitized_src)
    appendix_removed_chars = 0
    if appendix_span is not None:
        appendix_removed_chars = appendix_span[1] - appendix_span[0]
        sanitized_src = stub_out_appendix_boilerplate(sanitized_src, appendix_span)

    main_tex.write_text(sanitized_src)

    manifest = build_manifest(
        original_src=original_src,
        sanitized_src=sanitized_src,
        detected_placeholders=detected_placeholders,
        boilerplate_span=boilerplate_span,
        paper_dir=paper_dir,
        venue_hint=venue_hint,
        removed_headers=removed_headers,
        appendix_removed_chars=appendix_removed_chars,
    )
    write_template_manifest(paper_dir, manifest)
    return manifest
