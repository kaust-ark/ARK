---
name: page-adjustment
description: Strategies for fitting a paper to a venue's page limit without destroying content quality. Applies to any phase that modifies paper content.
tags: [writing, latex, page-limit, appendix, compression]
---

# Page Adjustment

## Guiding Principle

Page adjustment is surgery, not demolition. Every change must preserve the paper's technical accuracy, figure correctness, and citation integrity. The approach should be proportional to the gap — small gaps need small edits, large gaps may require structural changes.

## Assess the Gap First

Before making any change, compile and measure:
- **How many pages over/under?** This determines the scale of intervention.
- **Small gap (< 0.3 pages):** Tighten or loosen prose — a sentence here, a paragraph there. No structural changes needed.
- **Medium gap (0.3–1.0 pages):** May need to add/remove a paragraph, resize a figure, or move a subsection to appendix.
- **Large gap (> 1.0 pages):** Structural changes — move entire subsections to/from appendix, add/remove a discussion section, consolidate related work.

Match the intervention to the gap. Do not restructure the paper to fix a 0.2-page overshoot.

## When Adding New Content

When new experiment results or analyses need to be added (review phase, dev phase, any iteration):

1. **Consider appendix for details.** Full experiment methodology, detailed tables, and per-category breakdowns often fit better in `\appendix`, with a brief summary (1-3 sentences) and forward reference in the main body.
2. **Budget before writing.** Estimate how much space the new content needs. If the main body is already at or near the limit, the appendix is the natural home for details.
3. **Balance additions with removals.** If you add a paragraph, look for an equal-length passage that can be condensed or moved to appendix. Do this in the same edit — do not defer to a later compression pass.

## Compression Strategy (over limit) — use in priority order

Strategies are ranked by **information loss**, not by effort. **Exhaust
every option in a higher priority before touching the next priority.**
"Venue-limit at any cost" is not a valid excuse for jumping ahead; the
compression mechanism will keep looping until the body fits, so burning a
loop on a lossless strategy is always preferable to a lossy one.

### Priority 1 — LOSSLESS (same content, reshaped)

These move or compact content without deleting anything substantive. Try
all of these before Priority 2.

- **Move subsections to `\appendix`.** Detailed per-category breakdowns,
  extended ablations, full proofs/derivations, hyperparameter sweeps,
  prompt templates, implementation minutiae, dataset statistics beyond
  a summary — these belong in appendix. Keep a 1–3 sentence body
  summary with a forward reference. The body-page limit excludes
  appendix, so this is net-positive reader experience.
- **Merge short related subsections.** Two adjacent subsections that
  each have fewer than three sentences or that discuss the same
  concept can become one subsection with a single heading.
- **Tighten prose.** Remove hedging ("it is worth noting that…", "we
  note that…", "as a point of interest"), collapse two-item lists into
  inline text ("(i) A and (ii) B" → "A and B"), delete redundant
  restatements of a result already given elsewhere.

### Priority 2 — MINIMALLY LOSSY (small reformatting cost)

Only touch these after Priority 1 is exhausted.

- **Table font compression.** Use `\footnotesize` (or `\scriptsize` for
  wide tables with only numbers), remove redundant header rows. Do NOT
  remove data columns or rows.
- **Caption shortening.** Reduce captions to 1–2 sentences. Move the
  mechanistic explanation into the paragraph that first references the
  figure.

### Priority 3 — LOSSY, MANIFEST-GATED (last resort)

These change what the reader actually sees. **Consult
`figures/figure_manifest.json`'s `scalable` field before acting on any
figure** — the field exists precisely so you don't have to guess whether
shrinking is safe.

- **Shrink bitmap figures via `\includegraphics[width=…]`.** Only
  allowed when the manifest entry says `"scalable": true`. These are
  typically PaperBanana/Nano-Banana rendered concept figures; their
  labels are rasterized so a 10–20% reduction is fine.
- **Regenerate matplotlib figures with smaller `figsize`.** When a
  matplotlib figure is too wide/tall, open its plotting function in
  `scripts/create_paper_figures.py` and reduce `figsize` there, then
  re-run the script. Do NOT resize via `\includegraphics`
  (`scalable: false` in the manifest forbids this) — matplotlib bakes
  text into the PDF at authoring time, so shrinking via LaTeX makes
  labels unreadable at 48% scale.
- **Switch `figure*` ↔ `figure`.** A figure originally placed as
  `figure*` + `\textwidth` can be moved into a column if its aspect
  ratio and label density allow it after regeneration. Update the
  manifest's `placement` field to match.

### Never (regardless of priority)

- Delete figures, data rows, bars, subplots, or table entries — see
  figure-integrity skill.
- Delete `\cite{}` commands or modify `references.bib`.
- Exceed the venue page limit to preserve content. Desk rejection is
  worse than a sparse body; the hard ceiling is inviolable.

### Widow / Orphan check (always, after any compression pass)

If after compression the body ends by spilling **a single orphan
line** (a widow) onto the page beyond the limit — for example body
occupies pages 1 through N+1 where page N+1 has ≤3 lines of content
— do NOT respond by moving more content to appendix. Moving a
paragraph removes much more than the offending 1–3 lines and
produces an under-filled page N. Instead:

- Identify the paragraph whose tail landed on the overflow page (it
  is almost always the last body paragraph before references).
- Tighten that paragraph by 5–15 words: merge two short clauses,
  drop adverbial hedging, replace multi-word phrases ("on a held-out
  validation split" → "on held-out"), or collapse a trailing example
  list.
- Recompile; the orphan line should reflow into page N's last
  column.

A widow is a compile-level layout artefact, not a content problem.
Structural changes (move to appendix) are the wrong tool.

## Expansion Strategy (under limit)

- Deepen analysis: explain *why* results look the way they do, not
  just *what*
- Add related work: compare with 2–3 more relevant papers, specific
  technical differences
- Expand methodology: hyperparameters, hardware specs, dataset details
  that aid reproducibility
- Bring appendix content back: if content was moved to appendix during
  prior compression, selectively restore the most important parts
- Add a Limitations paragraph if one doesn't exist

## What to Avoid

- Avoid changing numbers in figures, tables, or quantitative claims — these come from result files
- Avoid removing figures or tables entirely when they can be moved to appendix instead
- Avoid deleting `\cite{}` commands or modifying `references.bib`
- Avoid adding filler text or restating what was already said
- Avoid fabricating new experimental results to fill space

## Sensitive Areas

These elements require extra care during page adjustment:

- **Figure data and labels** — sourced from `results/` files. If you must modify a figure, re-read the source data. Do not change values from memory.
- **Table values** — same principle. Move or resize the table, but be careful with its contents.
- **`references.bib`** — managed by the citation system. Prefer not to touch it.
- **Venue template** — `\documentclass`, `\usepackage`, `.sty`, `.bst` are off-limits.

## Document Structure

The correct ordering of sections at the end of the paper is:

1. Body text (up to page limit)
2. `\clearpage` + `\bibliography{references}` — references start on a new page
3. `\clearpage` + `\appendix` — appendix starts on a new page after references

Never place `\appendix` before `\bibliography`. When adding appendix content, verify this ordering is maintained.

## Verification After Adjustment

After every page adjustment pass:
1. Compile and check page count
2. Spot-check 2-3 quantitative values against `results/` files — especially any figure or table you touched
3. Check for LaTeX warnings on unresolved `\ref{}` or `\cite{}`
4. Read through any section you modified — no dangling references to moved content
5. Verify document structure: body → references → appendix (in that order)
