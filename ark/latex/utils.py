"""LaTeX-related utility functions."""
import os
import re
import subprocess
from pathlib import Path

_ENDMATTER_HEADING = re.compile(
    r"(?:(?:\d+|[IVX]+)[.\s]+)?(?:references|bibliography|acknowledg(?:e)?ments?"
    r"|appendices|appendix(?:\s+[a-z])?)", re.IGNORECASE)

# The destination travels with the delivered PDF, including when the project
# is exported without auxiliary TeX files. Older papers use visible headings.
BODY_END_DESTINATION = "idea2paper.body.end"
BODY_END_PDF_MARKER = rf"\ifdefined\pdfdest\pdfdest name{{{BODY_END_DESTINATION}}} xyz\relax\fi"


def marked_body_end_page(doc) -> int | None:
    destination = doc.resolve_names().get(BODY_END_DESTINATION)
    if destination and 0 <= destination.get("page", -1) < len(doc):
        return destination["page"] + 1
    return None


def recorded_body_end_page(aux_text: str, total_pages: int) -> int | None:
    """The body's last page from the compiler marker, when consistent with this PDF."""
    last = re.search(r"\\gdef\s*\\@abspage@last\{(\d+)\}", aux_text)
    if last and int(last[1]) != total_pages:
        return None
    marker = re.search(r"\\gdef\\arkBodyEndPage\{(\d+)\}", aux_text)
    if marker and 1 <= int(marker[1]) <= total_pages:
        return int(marker[1])
    return None


def body_page_boundary(doc) -> tuple[int, tuple[float, float]] | None:
    """Return the endmatter page index and preceding body text height in each column.

    A marker may precede a full page of deferred body floats flushed by clearpage.
    Look for the actual start of the endmatter from that marker onwards.
    """
    marker = marked_body_end_page(doc)
    for index in range(marker - 1 if marker else 0, len(doc)):
        page = doc[index]
        lines = page.get_text().splitlines()
        if any(line.strip().lower() in {"contents", "table of contents"} for line in lines):
            continue
        if not any(_ENDMATTER_HEADING.fullmatch(line.strip()) for line in lines):
            continue
        height = page.rect.height
        body_bottoms = [0.0, 0.0]
        # Text blocks follow PDF reading order, including the left column when
        # References starts at the top of the right column on the same page.
        for block in page.get_text("dict", flags=0)["blocks"]:
            for line in block.get("lines", []):
                content = "".join(s["text"] for s in line["spans"]).strip()
                left, _, _, bottom = line["bbox"]
                if _ENDMATTER_HEADING.fullmatch(content):
                    if marker and index + int(any(body_bottoms)) < marker:
                        continue
                    return index, (body_bottoms[0], body_bottoms[1])
                if (content and not content.isdigit()
                        and 0.08 * height < bottom < 0.96 * height):
                    column = int(left >= page.rect.width / 2)
                    body_bottoms[column] = max(body_bottoms[column], bottom)
    return (marker, (0.0, 0.0)) if marker else None


# --------------- Title generation helpers ---------------

_TITLE_MIN_LEN = 10
_TITLE_MAX_LEN = 200
_TITLE_MAX_RETRIES = 3

# Match the start of an active (non-commented) ``\title`` command, positioning
# the regex right before the opening ``{``. Active means the line is not a
# ``%`` comment; we verify this by requiring only whitespace before ``\title``.
_ACTIVE_TITLE_RE = re.compile(r'^(?P<indent>[ \t]*)\\title\s*(?=\{)', re.MULTILINE)


def replace_latex_title(src: str, new_title: str) -> str:
    """Replace the first active ``\\title{...}`` in LaTeX source.

    Walks balanced braces (respecting ``\\{``/``\\}`` escapes) so titles
    containing nested LaTeX commands like ``\\title{A \\emph{note}}`` are
    handled correctly. Commented-out occurrences (lines starting with ``%``)
    are skipped. Returns ``src`` unchanged if no active ``\\title`` is found.
    """
    for m in _ACTIVE_TITLE_RE.finditer(src):
        brace_start = m.end()
        if brace_start >= len(src) or src[brace_start] != '{':
            continue
        depth = 1
        i = brace_start + 1
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
        if depth == 0:
            # src[brace_start] == '{', src[i-1] == '}' — replace the body.
            return src[:brace_start + 1] + new_title + src[i - 1:]
    return src


def generate_title_via_llm(idea_text: str, timeout: int = 60) -> str:
    """Call ``claude -p`` to generate a paper title from idea text.

    Returns the title string, or "" on failure.  The prompt is tightly
    constrained: output ONLY the title, nothing else.
    """
    prompt = (
        "You are a scientific title generator. "
        "Given the research summary below, output EXACTLY ONE concise academic paper title.\n\n"
        "Rules:\n"
        "- Output ONLY the title text, nothing else\n"
        "- No quotes, no labels, no prefixes like 'Title:'\n"
        "- No explanation, no newlines, no markdown\n"
        "- Between 10 and 200 characters\n\n"
        f"Research summary:\n{idea_text[:4000]}"
    )
    try:
        from ark.llm_lite import complete, utility_model
        title = complete(prompt, model=utility_model(), timeout=timeout)
        if not title:
            return ""
        title = title.strip('"').strip("'").strip()
        # Strip common LLM prefix leaks
        for prefix in ("Title:", "title:", "Title :", "Generated title:"):
            if title.lower().startswith(prefix.lower()):
                title = title[len(prefix):].strip().strip('"').strip("'").strip()
        return title
    except subprocess.TimeoutExpired:
        return ""
    except FileNotFoundError:
        return ""


def validate_title(title: str) -> bool:
    """Check that a title is plausible."""
    if not title:
        return False
    if len(title) < _TITLE_MIN_LEN or len(title) > _TITLE_MAX_LEN:
        return False
    # Reject if it looks like LLM meta-output rather than a real title
    lower = title.lower()
    if any(phrase in lower for phrase in (
        "here is", "i suggest", "current title", "appropriate",
        "as requested", "certainly", "sure,",
    )):
        return False
    # Must contain at least one letter
    if not any(c.isalpha() for c in title):
        return False
    return True


def fallback_title_from_idea(idea_text: str) -> str:
    """Deterministic fallback: extract first substantive sentence from idea text."""
    for line in idea_text.splitlines():
        line = line.strip().lstrip("#").lstrip("-").lstrip("*").strip()
        if len(line) >= _TITLE_MIN_LEN and not line.startswith("```"):
            # Truncate to first sentence or max length
            for sep in (". ", "。", "! ", "? "):
                idx = line.find(sep)
                if 0 < idx < _TITLE_MAX_LEN:
                    line = line[:idx]
                    break
            if len(line) > _TITLE_MAX_LEN:
                line = line[:_TITLE_MAX_LEN - 3] + "..."
            return line
    # Absolute last resort
    return idea_text[:80].strip().replace("\n", " ")


def detect_latex_install_command() -> str:
    """Detect platform package manager and return texlive install command."""
    managers = [
        ("apt-get", "sudo apt-get install -y texlive-full"),
        ("dnf", "sudo dnf install -y texlive-scheme-full"),
        ("yum", "sudo yum install -y texlive-scheme-full"),
        ("pacman", "sudo pacman -S --noconfirm texlive-full"),
        ("brew", "brew install --cask mactex"),
    ]
    import shutil
    for cmd, install in managers:
        if shutil.which(cmd):
            return install
    return ""


def auto_fix_latex(latex_dir: Path, log_fn=None):
    """Programmatic fixes for common LaTeX compilation issues."""
    main_tex = latex_dir / "main.tex"
    if not main_tex.exists():
        return

    # Fix 1: Strip non-UTF8 bytes
    raw = main_tex.read_bytes()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        cleaned = raw.decode("utf-8", errors="ignore").encode("utf-8")
        main_tex.write_bytes(cleaned)
        if log_fn:
            log_fn("Auto-fix: stripped non-UTF8 bytes from main.tex", "INFO")

    # Fix 2: Same for .bib files
    for bib in latex_dir.glob("*.bib"):
        raw = bib.read_bytes()
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            cleaned = raw.decode("utf-8", errors="ignore").encode("utf-8")
            bib.write_bytes(cleaned)
            if log_fn:
                log_fn(f"Auto-fix: stripped non-UTF8 bytes from {bib.name}", "INFO")


def parse_overfull_warnings(latex_dir: Path) -> list[str]:
    """Parse main.log for Overfull hbox warnings."""
    log_path = latex_dir / "main.log"
    if not log_path.exists():
        return []
    try:
        log_text = log_path.read_text(errors="replace")
        warnings = []
        for line in log_text.splitlines():
            if 'Overfull \\hbox' in line:
                warnings.append(line.strip())
        return warnings
    except Exception:
        return []


def extract_latex_errors(log_path: Path) -> str:
    """Parse LaTeX log for structured error messages.

    Looks for common LaTeX error patterns. Returns up to 5
    error blocks with surrounding context.
    """
    if not log_path.exists():
        return "No main.log found — compilation may not have run."

    try:
        log_text = log_path.read_text(errors="replace")
    except Exception as e:
        return f"Could not read main.log: {e}"

    lines = log_text.splitlines()
    error_markers = []
    for i, line in enumerate(lines):
        if (line.startswith("!")
                or "Fatal error" in line
                or "Emergency stop" in line
                or "Undefined control sequence" in line
                or "LaTeX Error:" in line
                or "Package Error:" in line
                or "Missing $ inserted" in line
                or "Extra alignment tab" in line
                or "Misplaced alignment tab" in line
                or "Missing \\begin{document}" in line
                or "File not found" in line
                or "No file" in line and ".sty" in line
                or "Too many }'s" in line
                or "Runaway argument" in line
                or "Paragraph ended before" in line):
            error_markers.append(i)

    if not error_markers:
        return "Compilation failed but no specific errors found in main.log."

    # Deduplicate nearby markers (within 3 lines)
    deduped = [error_markers[0]]
    for idx in error_markers[1:]:
        if idx - deduped[-1] > 3:
            deduped.append(idx)
    error_markers = deduped[:5]  # Cap at 5 errors

    blocks = []
    for idx in error_markers:
        start = max(0, idx - 1)
        end = min(len(lines), idx + 4)
        block = "\n".join(lines[start:end])
        blocks.append(block)

    return f"Found {len(blocks)} error(s) in main.log:\n\n" + "\n---\n".join(blocks)
