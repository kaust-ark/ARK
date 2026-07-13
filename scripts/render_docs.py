#!/usr/bin/env python3
"""Render the website docs pages from the READMEs — one source of truth.

README.md / README_zh.md / README_ar.md are the documentation. This script
converts the shared portion of each (everything after the ``<!-- docs:start -->``
marker) into website/homepage/{,zh/,ar/}doc.html using the per-language shells
in scripts/doc_shell/ (site navbar/footer/styles preserved verbatim; the shell
carries ``{{TOC}}`` and ``{{CONTENT}}`` placeholders).

Deterministic output — tests/unit/test_docs_sync.py regenerates and compares,
so a README edit without re-running this script fails the suite:

    python scripts/render_docs.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = "<!-- docs:start -->"

TARGETS = [
    # (readme, shell, output, asset_prefix for repo-relative links)
    ("README.md",    "scripts/doc_shell/en.html", "website/homepage/doc.html",    ""),
    ("README_zh.md", "scripts/doc_shell/zh.html", "website/homepage/zh/doc.html", "../"),
    ("README_ar.md", "scripts/doc_shell/ar.html", "website/homepage/ar/doc.html", "../"),
]


def _slug(text: str) -> str:
    s = re.sub(r"<[^>]+>", "", text)
    s = re.sub(r"[^\w\s一-鿿؀-ۿ-]", "", s).strip().lower()
    return re.sub(r"[\s]+", "-", s) or "section"


def render_one(readme: Path, shell: Path, out: Path, asset_prefix: str) -> None:
    import markdown

    src = readme.read_text()
    if MARKER in src:
        src = src.split(MARKER, 1)[1]

    # Repo-relative artifacts don't exist on the website — point them at GitHub.
    src = re.sub(r"\]\((?!http|#|mailto)([^)]+\.(?:ya?ml|sh|md|py))\)",
                 r"](https://github.com/kaust-ark/ARK/blob/main/\1)", src)
    # GitHub-flavored alert blocks → plain emphasized paragraphs.
    src = re.sub(r"^> \[!(?:NOTE|TIP|IMPORTANT|WARNING)\]\s*$", ">", src, flags=re.M)

    html = markdown.markdown(
        src, extensions=["tables", "fenced_code", "toc", "md_in_html", "sane_lists"],
        extension_configs={"toc": {"slugify": lambda v, sep: _slug(v)}},
    )

    # Sidebar TOC from h2 headings.
    toc_items = []
    for m in re.finditer(r'<h2 id="([^"]+)">(.*?)</h2>', html):
        label = re.sub(r"<[^>]+>", "", m.group(2))
        toc_items.append(f'      <a href="#{m.group(1)}" class="doc-nav-link">{label}</a>')
    toc = "\n".join(toc_items)

    page = shell.read_text().replace("{{TOC}}", toc).replace("{{CONTENT}}", html)
    out.write_text(page)
    print(f"  {readme.name} -> {out.relative_to(ROOT)} "
          f"({len(html)} chars, {len(toc_items)} sections)")


def main() -> int:
    for readme, shell, out, prefix in TARGETS:
        render_one(ROOT / readme, ROOT / shell, ROOT / out, prefix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
