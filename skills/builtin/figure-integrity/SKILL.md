---
name: figure-integrity
description: Rules for maintaining figure and table data integrity. All visual data must trace to result files — never invented or modified for convenience.
tags: [writing, figures, tables, data-integrity, latex]
---

# Figure and Table Integrity

## Core Rule

Every number displayed in a figure or table must be traceable to a file in `results/` or `data/`. If the source file doesn't exist, the figure should not exist either.

## When Creating or Updating Figures

1. **Read the source data first.** Before writing any plotting script, read the actual result file (`results/*.json`, `results/*.csv`) and extract the exact values. Do NOT hardcode numbers from memory or from the paper text.
2. **Load data from files, not literals.** Plotting scripts must `json.load()` or `pandas.read_csv()` from the result file. Acceptable:
   ```python
   with open("results/exp3_repair_n100.json") as f:
       data = json.load(f)
   drr = data["mean_drr"]
   ```
   Not acceptable:
   ```python
   drr = 0.942  # from the paper
   ```
3. **Handle missing data honestly.** If a result file doesn't exist or is incomplete:
   - Leave the figure cell/bar/point blank or mark it "N/A"
   - Add a `% TODO` comment in the LaTeX caption
   - Do NOT invent a plausible value

## When the Paper Text Cites a Figure

The flow is always: **experiment → result file → figure → paper text**. Never the reverse.

- If the paper says "F1=0.891 (Figure 4)" — verify that Figure 4's plotting script reads a file that contains 0.891.
- If the paper text and the figure disagree, the result file is the authority. Fix whichever is wrong.

## During Page Adjustment

Figures and tables are **protected zones** during page compression or expansion:

- You may **resize** a figure (change `width`/`height` in `\includegraphics`)
- You may **move** a figure to a different position in the LaTeX source or to the appendix
- You may **change float placement** (`[t]`, `[!htbp]`, `[p]`)
- You must NOT **change the data** shown in the figure
- You must NOT **regenerate the figure with different values**
- You must NOT **remove data points, bars, or table rows** to make it smaller

## After Any Figure Modification

1. **Spot-check values.** Pick 2-3 numbers from the figure and verify them against the source file.
2. **Check captions.** If the figure was moved or its context changed, update the caption to remain accurate.
3. **Check references.** Ensure all `\ref{fig:...}` in the text still point to the correct figure.

## Common Mistakes to Avoid

- Regenerating a plotting script from scratch using numbers "remembered" from the paper text, when the original script loaded from a result file
- Changing figure data during page compression to make the figure smaller
- Copying a figure caption's numbers into a new version of the figure without reading the actual data
- Creating a figure for an experiment that was blocked or incomplete — an honest "not evaluated" note is better than a fabricated chart
