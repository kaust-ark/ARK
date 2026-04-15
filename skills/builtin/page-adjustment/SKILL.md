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

## Available Strategies

These are tools in your toolbox. Choose based on the gap size and paper structure — there is no fixed priority order.

**For compression (over limit):**
- Tighten prose: merge overlapping sentences, remove hedging ("it is worth noting that..."), collapse short lists into inline text
- Move subsections to `\appendix`: per-category breakdowns, detailed ablations, extended proofs, large tables — keep a summary in the body
- Compress tables: use `\footnotesize`, remove redundant columns, merge header rows
- Reduce figure size: decrease `height` in `\includegraphics` by 10-20%
- Merge sections: combine "Discussion" and "Conclusion" if both are short, merge sub-subsections with <3 sentences

**For expansion (under limit):**
- Deepen analysis: explain *why* results look the way they do, not just *what*
- Add related work: compare with 2-3 more relevant papers, specific technical differences
- Expand methodology: hyperparameters, hardware specs, dataset details that aid reproducibility
- Bring appendix content back: if content was moved to appendix during prior compression, selectively restore the most important parts
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
