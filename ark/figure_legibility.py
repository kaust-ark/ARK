"""Can a reader read the figure's own labels?

A delivered paper carried charts whose axis labels were drawn on top of one
another: `Raw` and `Standardized` collapsed into `RaStandardized`, three
category labels fused into `PlannAdtualActual max_iter max_iter`, two decade
ticks into `10⁰10²`, and a y-axis title arrived as `ter_ (optimizer
iterations; log` with both ends off the canvas. matplotlib does not complain
about any of this — it draws what it is told at the figure size it is given —
and nothing downstream looked. The reviewer was shown a page image with one of
them on it and wrote "Figure fonts are adequate (9-10pt). Layout is clean".
The structural checks passed too: the figures were the right size, inserted at
natural width, and every float was still referenced.

So this is the missing check, and it is deterministic. A vector figure carries
its text as positioned runs, so two labels that overprint are two boxes that
intersect, and a label the canvas cut off is a box that leaves the page.

Read the text with `get_texttrace`, not `get_text`
--------------------------------------------------
`get_text` merges neighbouring runs that share a baseline, a font and a size
into one span — and two tick labels are most likely to merge exactly when they
have run into each other, because nothing separates them any more. On
fig_n_iter_bar the page plainly reads `Raw featuresStandardized` and `get_text`
returned the single span `'Raw featuresStandardized'`: one span is no pair, so
the worst-looking collision in the corpus scored zero. The sibling figure was
caught only because its middle label happened to be bold. `get_texttrace`
reports one entry per show-text operation, which is one per matplotlib text
object, so the pair survives.

It also reports the box the glyphs actually occupy, where `get_text` pads every
box with the font's full ascent and descent. That padding was the reason a
legible "Value" sitting at the left edge overran its canvas by 2.1 pt while a
genuinely truncated label overran by 2.4 pt — indistinguishable, and the reason
this module used to corroborate clipping by rasterising the page and looking
for ink on the outermost pixel ring. With tight boxes the signal separates on
its own and the raster step is gone.

Calibration
-----------
Fifteen figures from six runs of the same project, every one rendered and read
by eye to label it, then measured:

    overlap as a fraction of the smaller box, for pairs that do NOT share a
    baseline (the shared-baseline rule below catches those):
        unreadable   0.22  0.29                    ('18' under a legend;
                                                     a bar label under its note)
        fine         0.01  0.02  0.04              (labels of different rows;
                                                     two axis titles 15 pt apart
                                                     vertically)

    vertical overlap as a fraction of the shorter box, i.e. "same row":
        unreadable   0.66  0.76  0.81  0.98  1.00
        fine         0.02  0.16  0.22  0.34

    how far a text box leaves the canvas:
        unreadable   5.3  9.4  10.3  28.4 pt
        fine         0.0 pt, on every clean figure

Each gap is wide and the thresholds sit inside them. Against those fifteen
labels the rule agrees on fifteen.

What this check does not see
----------------------------
Worth knowing, because every hole found so far was found by a real run rather
than by reasoning about one:

  - Rotated text is excluded from the collision rule, deliberately: turning a
    crowded label is the usual fix, and a turned glyph run has an axis-aligned
    box far larger than its ink, so flagging it would punish the remedy.
    Rotated text IS checked for clipping — a y-axis title is the label most
    likely to run off the canvas.
  - Labels that are merely adjacent. `'95.8'` and `'ΔAcc: +2.8 pp'` sit 6.4 pt
    apart on a figure that is fine; the rule is "overlapping", not "close".
  - Text drawn under an opaque patch — a value label behind a legend box is
    washed out with no text box to intersect.
  - Bitmaps, which carry no text to measure. `figures_not_crushed` covers
    those, and the delivery contract falls back to a figure's vector twin when
    the paper includes the raster.
"""
from __future__ import annotations

import itertools
from pathlib import Path

# Fraction of the smaller label's area that must be covered to count as an
# overprint. Measured, not guessed: see the calibration above.
OVERLAP_RATIO = 0.12

# Two labels sharing a baseline — a tick row, a legend — collide as soon as
# they overlap sideways at all, long before either covers that fraction of the
# other. `Standardized` and `Raw (uncensored)` ran into each other by 2.8 pt,
# which is 5% of an area and plainly broken on the page.
SHARED_BASELINE = 0.6   # vertical overlap, as a fraction of the shorter box

# How far a box may leave the canvas before the glyphs are really being cut.
# Every clean figure measured 0.0; the mildest real truncation was 5.3 pt.
EDGE_TOLERANCE = 1.0    # pt

# How many offending pairs to name. Four is enough to recognise the figure.
MAX_REPORTED = 4


def _drawn(pdf: Path) -> list:
    """(text, rect, direction) for every run of visible text in the figure.

    One entry per show-text operation, which is one per matplotlib text object.
    """
    import fitz

    out = []
    doc = fitz.open(str(pdf))
    try:
        for page in doc:
            for run in page.get_texttrace():
                if run.get("type", 0) == 3:     # clip-only text, never painted
                    continue
                text = "".join(chr(c[0]) for c in run["chars"]).strip()
                if text:
                    out.append((text,
                                fitz.Rect(run["bbox"]).normalize(),
                                tuple(run.get("dir", (1, 0)))))
    finally:
        doc.close()
    return out


def collisions(pdf: Path, ratio: float = OVERLAP_RATIO) -> list:
    """Label pairs that overprint, worst first, as (text, text, fraction)."""
    try:
        runs = _drawn(Path(pdf))
    except Exception:
        return []                      # unreadable figure is not this check's call
    upright = [(t, r) for t, r, direction in runs if direction == (1, 0)]
    found = []
    for (t1, r1), (t2, r2) in itertools.combinations(upright, 2):
        inter = r1 & r2
        if inter.is_empty or inter.width <= 0:
            continue
        smaller = min(r1.get_area(), r2.get_area())
        if smaller <= 0:
            continue
        share = inter.get_area() / smaller
        same_row = inter.height > SHARED_BASELINE * min(r1.height, r2.height)
        if share >= ratio or same_row:
            found.append((t1, t2, round(share, 2)))
    found.sort(key=lambda row: -row[2])
    return found


def clipped(pdf: Path, tolerance: float = EDGE_TOLERANCE) -> list:
    """Labels the figure's own canvas cut off, as (text, edge).

    `n_iter_ (optimizer iterations; log scale)` is written to the file whole
    and arrives on the page as `ter_ (optimizer iterations; log`, because the
    glyphs outside the canvas are simply never shown. What gives it away is the
    box: it starts 28 pt above the top edge and ends 14 pt below the bottom.
    """
    try:
        runs = _drawn(Path(pdf))
        import fitz
        doc = fitz.open(str(pdf))
        page_rect = doc[0].rect
        doc.close()
    except Exception:
        return []
    out = []
    for text, r, _ in runs:
        for edge, escape in (("left", page_rect.x0 - r.x0),
                             ("right", r.x1 - page_rect.x1),
                             ("top", page_rect.y0 - r.y0),
                             ("bottom", r.y1 - page_rect.y1)):
            if escape > tolerance:
                out.append((text, edge))
                break
    return out


def describe(pdf: Path, ratio: float = OVERLAP_RATIO) -> str:
    """One line naming what is unreadable, or "" when the figure is clean."""
    parts = []
    rows = collisions(pdf, ratio)
    if rows:
        pairs = ", ".join(f"{a!r}/{b!r}" for a, b, _ in rows[:MAX_REPORTED])
        more = f" (+{len(rows) - MAX_REPORTED} more)" if len(rows) > MAX_REPORTED else ""
        parts.append(f"labels overprint: {pairs}{more}")
    cut = clipped(pdf)
    if cut:
        named = ", ".join(f"{t!r} ({e})" for t, e in cut[:MAX_REPORTED])
        more = f" (+{len(cut) - MAX_REPORTED} more)" if len(cut) > MAX_REPORTED else ""
        parts.append(f"labels cut off by the figure edge: {named}{more}")
    return "; ".join(parts)
