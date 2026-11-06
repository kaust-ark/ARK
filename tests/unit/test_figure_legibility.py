"""A figure whose own labels overprint is caught before it is delivered.

From a real run: two delivered charts read "RaStandardized" and
"PlannAdtualActual max_iter max_iter", and every existing figure check passed
on them — they were the right size, inserted at natural width, and every
float was referenced. The reviewer was shown a page image carrying the first
one and called the layout clean.

Thresholds here are the measured ones. Across four figures from two runs of
the same project the overprinted pairs covered 0.31-0.72 of the smaller
label and the clean ones never exceeded 0.14.
"""
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

from ark.figure_legibility import collisions, describe


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


def test_an_unreadable_file_is_not_this_check_s_call(tmp_path):
    broken = tmp_path / "notapdf.pdf"
    broken.write_text("this is not a PDF")

    assert collisions(broken) == []


def test_the_description_names_the_labels_and_counts_the_rest(tmp_path):
    f = _figure(tmp_path / "many.pdf",
                [(f"label{i}", 40 + i, 150) for i in range(6)])

    text = describe(f)

    assert "label0" in text and "more)" in text
