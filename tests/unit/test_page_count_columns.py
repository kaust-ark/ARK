"""Regression tests for column-aware body page count.

The page-counter previously over-reported fill on two-column papers
whose last body page had only one column occupied (the other column
empty). It treated the aux-recorded y position as a 1-D fill ratio,
which is correct for single-column docs but wrong when half the page
is empty whitespace.

These tests verify _column_adjust and _is_two_column_doc on stub
fitz-like objects.
"""

import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _block(x0, y0, x1, y1, text=""):
    """fitz get_text('blocks') row format: (x0, y0, x1, y1, text, no, type)."""
    return (x0, y0, x1, y1, text, 0, 0)


def _stub_page(blocks, width=612, height=792):
    page = types.SimpleNamespace()
    page.rect = types.SimpleNamespace(width=width, height=height)
    page.get_text = lambda kind: blocks
    return page


class _StubDoc:
    """Minimal fitz.Document stand-in for the fields _column_adjust uses."""
    def __init__(self, pages):
        self._pages = pages
        self.page_count = len(pages)

    def __getitem__(self, i):
        return self._pages[i]

    def __len__(self):
        return self.page_count

    def __iter__(self):
        return iter(self._pages)

    def close(self):
        pass


def _stub_doc(pages):
    return _StubDoc(pages)


@pytest.fixture
def compiler():
    from ark.latex.compiler import CompilerMixin

    stub = MagicMock(spec=CompilerMixin)
    stub.latex_dir = Path("/tmp/notused")
    stub.log = MagicMock()
    # Bind the methods we want to test
    stub._column_adjust = CompilerMixin._column_adjust.__get__(stub)
    stub._is_two_column_doc = CompilerMixin._is_two_column_doc.__get__(stub)
    stub._HEADER_BAND = CompilerMixin._HEADER_BAND
    stub._FOOTER_BAND = CompilerMixin._FOOTER_BAND
    return stub


# ── _is_two_column_doc ───────────────────────────────────────────────


def test_is_two_column_detects_2col_layout(compiler):
    # Sample (page index 1) has content in both halves of the page.
    sample = _stub_page([
        _block(54, 100, 290, 700, "left col body"),
        _block(322, 100, 558, 700, "right col body"),
    ])
    last = _stub_page([_block(54, 100, 290, 600, "last")])
    doc = _stub_doc([sample, sample, last])
    # ref_page_idx = 2 (so last_body_idx = 1)
    assert compiler._is_two_column_doc(doc, ref_page_idx=2) is True


def test_is_two_column_rejects_1col_layout(compiler):
    # Sample has only left-half content (single-column flow).
    sample = _stub_page([_block(72, 100, 540, 700, "single col body")])
    doc = _stub_doc([sample, sample])
    # The block starts at x=72 < mid_x=306 → only left is_filled, no right.
    # Wait: depends on b[0]. Block left edge 72 < mid_x=306 → "left filled".
    # Block right edge 540 > mid_x but classification is by b[0] only.
    # So has_right=False → not 2-col.
    assert compiler._is_two_column_doc(doc, ref_page_idx=1) is False


def test_is_two_column_short_doc_returns_false(compiler):
    # < 2 pages → can't sample.
    page = _stub_page([_block(54, 100, 290, 600, "body")])
    doc = _stub_doc([page])
    assert compiler._is_two_column_doc(doc, ref_page_idx=0) is False


# ── _column_adjust ───────────────────────────────────────────────────


def test_column_adjust_right_empty_uses_area_average(compiler):
    # 2-col last page with body in left col only (right col empty).
    # area-average = (left_y/ph + 0) / 2 = (696/792 + 0) / 2 ≈ 0.439
    sample = _stub_page([
        _block(54, 100, 290, 700, "L"),
        _block(322, 100, 558, 700, "R"),
    ])
    last = _stub_page([
        _block(486, 48, 558, 56, "Anonymous Author(s)"),  # header (filtered)
        _block(54, 100, 290, 696, "long left col body"),
    ])
    doc = _stub_doc([sample, last])

    adjusted, note = compiler._column_adjust(
        doc, last_body_idx=1, ref_page_idx=2, fill_ratio=0.87,
    )
    assert adjusted == pytest.approx(0.4394, abs=0.001)
    assert "2-col area-avg" in note


def test_column_adjust_left_empty_uses_area_average(compiler):
    # Symmetric: body in right col only.
    sample = _stub_page([
        _block(54, 100, 290, 700, "L"),
        _block(322, 100, 558, 700, "R"),
    ])
    last = _stub_page([
        _block(322, 100, 558, 696, "long right col body"),
    ])
    doc = _stub_doc([sample, last])

    adjusted, note = compiler._column_adjust(
        doc, last_body_idx=1, ref_page_idx=2, fill_ratio=0.87,
    )
    assert adjusted == pytest.approx(0.4394, abs=0.001)
    assert "2-col area-avg" in note


def test_column_adjust_both_partial_uses_area_average(compiler):
    # The case the old halving logic missed: left full, right partial.
    # Visually ~70% used; area-average ≈ (91% + 50%) / 2 = 70.5%.
    sample = _stub_page([
        _block(54, 100, 290, 700, "L"),
        _block(322, 100, 558, 700, "R"),
    ])
    # left_y = 720 → 720/792 ≈ 91%; right_y = 396 → 396/792 = 50%
    last = _stub_page([
        _block(54, 100, 290, 720, "left col full"),
        _block(322, 100, 558, 396, "right col half"),
    ])
    doc = _stub_doc([sample, last])

    adjusted, note = compiler._column_adjust(
        doc, last_body_idx=1, ref_page_idx=2, fill_ratio=0.50,
    )
    # (720/792 + 396/792) / 2 = (0.909 + 0.5) / 2 = 0.7045
    assert adjusted == pytest.approx(0.7045, abs=0.001)
    assert "2-col area-avg" in note


def test_column_adjust_both_full_uses_area_average(compiler):
    # Both columns end at the same y → area-average ≈ raw fill.
    sample = _stub_page([
        _block(54, 100, 290, 700, "L"),
        _block(322, 100, 558, 700, "R"),
    ])
    last = _stub_page([
        _block(54, 100, 290, 600, "L"),
        _block(322, 100, 558, 600, "R"),
    ])
    doc = _stub_doc([sample, last])

    adjusted, note = compiler._column_adjust(
        doc, last_body_idx=1, ref_page_idx=2, fill_ratio=0.87,
    )
    # 600/792 = 0.7576; (0.7576 + 0.7576) / 2 = 0.7576
    assert adjusted == pytest.approx(0.7576, abs=0.001)
    assert "2-col area-avg" in note


def test_column_adjust_no_change_for_1col_doc(compiler):
    # 1-col layout (sample shows only left half occupied → not 2-col)
    sample = _stub_page([_block(72, 100, 540, 700, "single col")])
    last = _stub_page([_block(72, 100, 540, 600, "single col last")])
    doc = _stub_doc([sample, last])

    adjusted, note = compiler._column_adjust(
        doc, last_body_idx=1, ref_page_idx=2, fill_ratio=0.87,
    )
    assert adjusted == pytest.approx(0.87)
    assert note == ""


def test_column_adjust_excludes_running_header(compiler):
    # Header in right column must NOT count as "right column filled".
    sample = _stub_page([
        _block(54, 100, 290, 700, "L"),
        _block(322, 100, 558, 700, "R"),
    ])
    # 8% of 792 = 63.4; place header from y=48 to y=56 (entirely in band)
    last = _stub_page([
        _block(486, 48, 558, 56, "RunningHeader"),  # in top 8% band → filtered
        _block(54, 100, 290, 696, "left col body"),
    ])
    doc = _stub_doc([sample, last])

    adjusted, note = compiler._column_adjust(
        doc, last_body_idx=1, ref_page_idx=2, fill_ratio=0.87,
    )
    # Header excluded → right_y = 0 → average = (696/792 + 0) / 2 ≈ 0.439
    assert adjusted == pytest.approx(0.4394, abs=0.001)
    assert "2-col area-avg" in note
