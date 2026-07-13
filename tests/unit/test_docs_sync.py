"""Website docs must be regenerated whenever the READMEs change.

README.md/_zh/_ar are the single source of truth; website/homepage/*doc.html
are build artifacts of scripts/render_docs.py. Editing a README without
re-rendering leaves the site stale — this test regenerates into a temp tree
and compares byte-for-byte.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("markdown")

ROOT = Path(__file__).resolve().parents[2]
PAGES = ["website/homepage/doc.html",
         "website/homepage/zh/doc.html",
         "website/homepage/ar/doc.html"]


def test_docs_in_sync(tmp_path):
    # Work on a copy so the real tree is never touched by the test.
    for rel in ("README.md", "README_zh.md", "README_ar.md"):
        shutil.copy(ROOT / rel, tmp_path / rel)
    (tmp_path / "scripts").mkdir()
    shutil.copytree(ROOT / "scripts" / "doc_shell", tmp_path / "scripts" / "doc_shell")
    shutil.copy(ROOT / "scripts" / "render_docs.py", tmp_path / "scripts" / "render_docs.py")
    for rel in PAGES:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("placeholder")

    r = subprocess.run([sys.executable, str(tmp_path / "scripts" / "render_docs.py")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    for rel in PAGES:
        got = (tmp_path / rel).read_text()
        want = (ROOT / rel).read_text()
        assert got == want, (
            f"{rel} is stale — README changed without re-rendering. "
            f"Run: python scripts/render_docs.py")
