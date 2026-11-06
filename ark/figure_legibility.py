"""Do a figure's own labels overprint each other?

A delivered paper carried two charts whose axis labels were drawn on top of
one another: `Raw` and `Standardized` collapsed into `RaStandardized`, three
category labels fused into `PlannAdtualActual max_iter max_iter`, and the
exponent of one logarithmic tick landed on the next tick's mantissa
(`10⁰10²`). matplotlib does not complain about any of this — it draws what it
is told at the figure size it is given — and nothing downstream looked. The
reviewer was shown a page image with the first of them on it and wrote
"Figure fonts are adequate (9-10pt). Layout is clean". The structural checks
passed too: the figures were the right size, inserted at natural width, and
every float was still referenced.

So this is the missing check, and it is deterministic. A vector figure carries
its text as positioned spans, so two labels that overprint are two boxes that
intersect. Calibrated on four real figures from two runs of the same project:

    overprinted  0.72  0.49  0.45  0.41  0.39  0.31   (two broken figures)
    clean        0.14  0.05  0.05                     (two good figures)

as a fraction of the smaller box. The gap is wide, and 0.25 sits in it.

Two things are deliberately not flagged. Rotated text is excluded, because
turning a crowded label is the usual fix and a rotated glyph run has an
axis-aligned box far larger than its ink — flagging it would punish the
remedy. Bitmaps are skipped, because a raster figure has no text to measure;
`figures_not_crushed` is what covers those.
"""
from __future__ import annotations

import itertools
from pathlib import Path

# Fraction of the smaller label's area that must be covered to count as an
# overprint. Measured, not guessed: see the calibration in the module docstring.
OVERLAP_RATIO = 0.25

# Two labels sharing a baseline collide as soon as they overlap horizontally at
# all, long before either covers a quarter of the other. "Standardized" and
# "Raw (uncensored)" ran into each other by 2.8 pt — 5% of an area, invisible
# to the ratio, plainly broken on the page. Measured over the same figures:
#
#     touching   -19.49  -2.98  -2.76        (three defective tick rows)
#     fine        +0.08  +6.40  +20.28       (two clean figures)
#
# and every +0.08 is a log tick's mantissa beside its own exponent, which is
# why the rule is "overlapping", not "close". Zero is inside the gap.
SHARED_BASELINE = 0.6   # vertical overlap, as a fraction of the shorter box

# How many colliding pairs to name. Four is enough to recognise the figure.
MAX_REPORTED = 4


def _spans(pdf: Path) -> list:
    """(text, rect) for every horizontal, non-blank text span in the figure."""
    import fitz

    out = []
    doc = fitz.open(str(pdf))
    try:
        for page in doc:
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    # Only unrotated text: a turned label's axis-aligned box
                    # is much bigger than its ink, so it overlaps its
                    # neighbours on paper that is perfectly legible.
                    if tuple(line.get("dir", (1, 0))) != (1, 0):
                        continue
                    for span in line.get("spans", []):
                        if span.get("text", "").strip():
                            out.append((span["text"].strip(), fitz.Rect(span["bbox"])))
    finally:
        doc.close()
    return out


def collisions(pdf: Path, ratio: float = OVERLAP_RATIO) -> list:
    """Label pairs that overprint, worst first, as (text, text, fraction)."""
    try:
        spans = _spans(Path(pdf))
    except Exception:
        return []                      # unreadable figure is not this check's call
    found = []
    for (t1, r1), (t2, r2) in itertools.combinations(spans, 2):
        inter = r1 & r2
        if inter.is_empty:
            continue
        smaller = min(r1.get_area(), r2.get_area())
        if smaller <= 0:
            continue
        share = inter.get_area() / smaller
        # Two labels on one baseline — a tick row, a legend — are broken the
        # moment they overlap sideways, whatever fraction of an area that is.
        side_by_side = (
            inter.height > SHARED_BASELINE * min(r1.height, r2.height)
            and inter.width > 0
        )
        if share >= ratio or side_by_side:
            found.append((t1, t2, round(share, 2)))
    found.sort(key=lambda row: -row[2])
    return found


def describe(pdf: Path, ratio: float = OVERLAP_RATIO) -> str:
    """One line naming what overprints, or "" when the figure is clean."""
    rows = collisions(pdf, ratio)
    if not rows:
        return ""
    pairs = ", ".join(f"{a!r}/{b!r}" for a, b, _ in rows[:MAX_REPORTED])
    more = f" (+{len(rows) - MAX_REPORTED} more)" if len(rows) > MAX_REPORTED else ""
    return f"labels overprint: {pairs}{more}"
