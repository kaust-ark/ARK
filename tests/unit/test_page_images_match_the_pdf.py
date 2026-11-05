"""The page images shown to the reviewer are the pages the PDF actually has.

Page compression routinely shortens a paper. The image writer only ever wrote
page_01..page_N, so the image of a dropped page stayed on disk, and the
up-to-date shortcut in `_maybe_generate_page_images` returns whatever
`page_*.png` globs. The reviewer would then be handed a phantom page — in
practice the near-empty trailing one that compression had just cut, which is
exactly the kind of thing a review files an issue about.

Seen on a cheap test: "Generated 4 page images", then a 3-page final PDF with
page_04.png still sitting beside it.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

fitz = pytest.importorskip("fitz")


def _write_pdf(path: Path, pages: int):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), f"page {i + 1}")
    doc.save(str(path))
    doc.close()


@pytest.fixture
def compiler(tmp_path):
    from ark.latex.compiler import CompilerMixin

    stub = MagicMock(spec=CompilerMixin)
    stub.latex_dir = tmp_path
    stub.log = MagicMock()
    stub.pdf_to_images = CompilerMixin.pdf_to_images.__get__(stub)
    stub._maybe_generate_page_images = \
        CompilerMixin._maybe_generate_page_images.__get__(stub)
    return stub


def _images(d: Path):
    return sorted(p.name for p in d.glob("page_*.png"))


def test_a_shorter_pdf_leaves_no_image_of_the_page_it_lost(compiler, tmp_path):
    _write_pdf(tmp_path / "main.pdf", 4)
    assert len(compiler.pdf_to_images()) == 4
    assert _images(tmp_path) == ["page_01.png", "page_02.png",
                                 "page_03.png", "page_04.png"]

    _write_pdf(tmp_path / "main.pdf", 3)          # compression cut a page
    out = compiler.pdf_to_images()

    assert len(out) == 3
    assert _images(tmp_path) == ["page_01.png", "page_02.png", "page_03.png"]


def test_the_up_to_date_shortcut_cannot_return_a_phantom_page(compiler, tmp_path):
    """The shortcut globs the directory, so cleanup is what makes it honest."""
    _write_pdf(tmp_path / "main.pdf", 4)
    compiler.pdf_to_images()
    _write_pdf(tmp_path / "main.pdf", 3)
    compiler.pdf_to_images()

    # Second ask with no recompile in between: this is the path that used to
    # hand the reviewer four images for a three-page paper.
    again = compiler._maybe_generate_page_images()
    assert len(again) == 3


def test_a_longer_pdf_keeps_every_page(compiler, tmp_path):
    _write_pdf(tmp_path / "main.pdf", 2)
    compiler.pdf_to_images()
    _write_pdf(tmp_path / "main.pdf", 5)

    assert len(compiler.pdf_to_images()) == 5
    assert len(_images(tmp_path)) == 5


def test_an_unrelated_png_is_left_alone(compiler, tmp_path):
    (tmp_path / "page_notes.png").write_bytes(b"not a page image")
    _write_pdf(tmp_path / "main.pdf", 1)

    compiler.pdf_to_images()

    assert (tmp_path / "page_notes.png").exists()
