"""A figure whose own labels are unreadable is caught before it is delivered.

From real runs: delivered charts read "RaStandardized",
"PlannAdtualActual max_iter max_iter", "10⁰10²", and a y-axis title that
arrived as "ter_ (optimizer iterations; log". Every existing figure check
passed on them — they were the right size, inserted at natural width, and
every float was referenced. The reviewer was shown a page image carrying one
of them and called the layout clean.

Thresholds here are the measured ones, from fifteen figures across six runs of
the same project, each one rendered and read by eye before being labelled.
See the calibration table in `ark.figure_legibility`.
"""
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

from ark.figure_legibility import OVERLAP_RATIO, clipped, collisions, describe


def _figure(path: Path, labels, rotate=0):
    """A one-page PDF with `labels` as (text, x, y), like a chart's tick row."""
    doc = fitz.open()
    page = doc.new_page(width=240, height=160)
    for text, x, y in labels:
        page.insert_text((x, y), text, fontsize=9, rotate=rotate)
    doc.save(str(path))
    doc.close()
    return path


def test_two_tick_labels_drawn_on_top_of_each_other_are_caught(tmp_path):
    # "Raw" and "Standardized" sharing a baseline: the real failure.
    f = _figure(tmp_path / "bad.pdf", [("Raw", 40, 150), ("Standardized", 44, 150)])

    rows = collisions(f)

    assert rows, "overprinted labels were not reported"
    assert {rows[0][0], rows[0][1]} == {"Raw", "Standardized"}
    assert rows[0][2] >= 0.25
    assert "labels overprint" in describe(f)


def test_labels_that_merely_sit_next_to_each_other_are_left_alone(tmp_path):
    # A tick row with normal spacing, and a value label above a bar: the
    # shapes that scored 0.05-0.14 on the figures that were fine.
    f = _figure(tmp_path / "ok.pdf",
                [("Raw", 40, 150), ("Standardized", 110, 150), ("2718", 60, 40)])

    assert collisions(f) == []
    assert describe(f) == ""


def test_turning_a_crowded_label_is_not_reported_as_a_collision(tmp_path):
    # Rotation is the usual remedy. A turned glyph run has an axis-aligned
    # box far larger than its ink, so comparing those boxes would flag the
    # fix as the defect.
    f = _figure(tmp_path / "rot.pdf",
                [("Standardized", 40, 150), ("Raw", 48, 150)], rotate=90)

    assert collisions(f) == []


def test_a_figure_with_no_text_at_all_is_not_a_collision(tmp_path):
    f = _figure(tmp_path / "blank.pdf", [])

    assert collisions(f) == [] and describe(f) == ""


def test_an_unreadable_file_is_reported_as_unreadable_not_as_clean(tmp_path):
    """It is still not this check's call — which is why it must propagate.

    This used to assert `collisions(broken) == []`, and the name said that an
    unreadable file was "not this check's call". The return value could not
    carry that distinction: every caller read `[]` as "no overlapping labels",
    and `describe()` returned "" for a figure nobody had managed to open. The
    only way to leave the decision to the caller is to let the caller see it.
    """
    broken = tmp_path / "notapdf.pdf"
    broken.write_text("this is not a PDF")

    with pytest.raises(Exception):
        collisions(broken)
    with pytest.raises(Exception):
        clipped(broken)
    with pytest.raises(Exception):
        describe(broken)


def test_the_description_names_the_labels_and_counts_the_rest(tmp_path):
    f = _figure(tmp_path / "many.pdf",
                [(f"label{i}", 40 + i, 150) for i in range(6)])

    text = describe(f)

    assert "label0" in text and "more)" in text


def test_tick_labels_in_one_font_that_run_into_each_other_are_caught(tmp_path):
    """The exact shape that escaped: one row, one font, one size, touching.

    fig_n_iter_bar's x-axis reads "Raw featuresStandardized" — the two tick
    labels overlap by 2.6 pt, which is 5% of either box and far under the area
    threshold. Two labels sharing a baseline collide as soon as they meet, so
    the shared-baseline rule is what has to catch this one.

    It is also why this module reads text with `get_texttrace`. `get_text`
    merges neighbours that share a baseline, a font and a size into a single
    span, and two tick labels merge precisely when they have run together and
    nothing separates them any more: the page plainly read
    "Raw featuresStandardized" and `get_text` returned it as one span, so there
    was no pair to measure and the worst collision in the corpus scored zero.
    The earlier version of this test used two different font sizes, which kept
    the spans apart and hid that for a full run.
    """
    doc = fitz.open()
    page = doc.new_page(width=240, height=160)
    page.insert_text((20, 150), "Raw features", fontsize=9)
    # 2.6 pt into its neighbour, the overlap measured on the real figure
    page.insert_text((70.4, 150), "Standardized", fontsize=9)
    doc.save(str(tmp_path / "touch.pdf"))
    doc.close()

    rows = collisions(tmp_path / "touch.pdf")

    assert rows, "labels running into each other were not reported"
    assert {rows[0][0], rows[0][1]} == {"Raw features", "Standardized"}
    assert rows[0][2] < OVERLAP_RATIO, "this pair is below the area threshold by design"


def test_a_log_tick_beside_its_own_exponent_is_not_a_collision(tmp_path):
    """`10` and its superscript `2` sit 0.08 pt apart in every clean figure.

    They share a baseline band, so the shared-baseline rule has to leave them
    alone — which it does, because adjacent is not overlapping.
    """
    doc = fitz.open()
    page = doc.new_page(width=240, height=160)
    page.insert_text((40, 150), "10", fontsize=9)
    # x = the mantissa's right edge + the 0.08 pt measured in the real figures
    page.insert_text((50.09, 145), "2", fontsize=6)
    doc.save(str(tmp_path / "log.pdf"))
    doc.close()

    assert collisions(tmp_path / "log.pdf") == []


def _edge_figure(path, text, x, y, size=9):
    """A figure whose label is placed so its glyphs run off the canvas."""
    doc = fitz.open()
    page = doc.new_page(width=120, height=60)
    page.insert_text((x, y), text, fontsize=size)
    doc.save(str(path))
    doc.close()
    return path


def test_a_label_whose_glyphs_run_off_the_canvas_is_caught(tmp_path):
    """`n_iter_ (optimizer iterations; log scale)` arrived as `ter_ (optimizer…`.

    matplotlib drew it, the canvas cut it, and nothing downstream noticed:
    the paper shipped with a y-axis nobody can read.
    """
    f = _edge_figure(tmp_path / "cut.pdf", "optimizer iterations; log scale", -40, 30)

    cut = clipped(f)

    assert cut, "a label running off the canvas was not reported"
    assert cut[0][1] == "left"
    assert "cut off by the figure edge" in describe(f)


def test_a_label_that_merely_sits_at_the_edge_is_left_alone(tmp_path):
    """"Value" sitting against the left edge is legible, and must stay quiet.

    Under `get_text` its box overran the canvas by 2.1 pt against a truncated
    label's 2.4 pt — indistinguishable, which is why this check once had to
    corroborate clipping by rasterising the page and looking for ink on the
    outermost pixel ring. `get_texttrace` reports the glyphs' own extent with
    no ascent/descent padding, so this label does not leave the canvas at all
    and the raster step is gone.
    """
    f = _edge_figure(tmp_path / "edge.pdf", "Value", 2, 30)

    assert clipped(f) == []
    assert describe(f) == ""


def test_both_kinds_of_unreadable_label_are_named_together(tmp_path):
    doc = fitz.open()
    page = doc.new_page(width=120, height=60)
    page.insert_text((-40, 20), "optimizer iterations; log scale", fontsize=9)
    page.insert_text((20, 50), "Standardized", fontsize=9)
    page.insert_text((60, 50), "Raw (uncensored)", fontsize=8)
    doc.save(str(tmp_path / "both.pdf"))
    doc.close()

    text = describe(tmp_path / "both.pdf")

    assert "labels overprint" in text and "cut off by the figure edge" in text
