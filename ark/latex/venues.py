"""Locate the published venue assets used to seed and verify manuscripts."""
from pathlib import Path
import re


def venue_templates():
    package = Path(__file__).resolve().parents[1]
    bundled = package / "bundle_assets" / "venue_templates"
    return bundled if bundled.is_dir() else package.parent / "venue_templates"


# Earlier projects keep their original venue contract on resume. These pairs
# are retained byte-for-byte with the current official kit for verification.
ICML_STYLES = ("icml2027.sty", "icml2026.sty")
FOUR_PAGE_PUBLICATIONS = 10
FOUR_PAGE_COLUMN_GAP_PT = 36


def initial_icml_style(paper):
    main = paper / "main.tex"
    if main.is_file():
        text = re.sub(r"(?m)(?<!\\)%.*$", "", main.read_text(errors="replace"))
        for name in ICML_STYLES:
            if re.search(r"\\usepackage\s*(?:\[[^\]]*\]\s*)?\{" + re.escape(Path(name).stem) + r"\}", text):
                return name
    return next((name for name in ICML_STYLES if (paper / name).is_file()), ICML_STYLES[0])


def icml_submission_statements(pdf):
    """Verify that the new submission kit's two declarations made it into print."""
    import fitz

    with fitz.open(pdf) as doc:
        body = "\n".join(page.get_text() for page in doc)
    before_refs = re.split(r"(?im)^\s*References\s*$", body, maxsplit=1)[0]
    headings = r"^\s*(AI Use Statement|Impact Statement|Acknowledg(?:e)?ments)\s*$"
    sections = re.split(headings, before_refs, flags=re.I | re.M)
    statements = {name.lower(): text for name, text in zip(sections[1::2], sections[2::2])}
    issues = []
    for title in ("AI Use Statement", "Impact Statement"):
        # Strip margin line numbers, page numbers and punctuation. A comment in
        # the skeleton cannot supply the statement's printed content.
        if not re.search(r"[^\W\d_]+\s+[^\W\d_]+", statements.get(title.lower(), "")):
            issues.append(f"ICML 2027 requires a substantive {title} before References. "
                          "Describe the actual AI assistance or this work's specific impacts.")
    return issues
