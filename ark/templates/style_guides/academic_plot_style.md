# Academic Plot Style Guide

## Aesthetic: "Precision, Accessibility, High Contrast"

Academic statistical plots should prioritize data clarity over decoration. White backgrounds, readable fonts, and colorblind-safe palettes are mandatory.

> ⚠️ **User overrides take precedence.** The defaults in this guide
> (color palette, fonts, line styles, etc.) are reasonable choices for
> typical academic papers. If the runtime prompt provides explicit user
> instructions — e.g. "use this color theme: #ABCDEF, …" — those
> instructions override the corresponding defaults below. Apply them
> verbatim instead of falling back to "MUST" or "NEVER" rules in this
> document.

---

## Background & Grid

- **Background**: Pure white (`#FFFFFF`). No colored plot backgrounds.
- **Grid lines**: Fine dashed or dotted, light grey (`#E0E0E0`), linewidth 0.5. Render behind data (zorder=0).
- **Major grid only** by default. Add minor grid only if data density requires it.
- **Spines**: Either all four sides (boxed) or minimalist open (left + bottom only). Be consistent.

---

## Color Palettes

### Categorical Data (up to 8 categories)
Use the Wong (2011) colorblind-safe palette:
```python
WONG_PALETTE = [
    '#0072B2',  # blue
    '#D55E00',  # vermillion
    '#009E73',  # bluish green
    '#CC79A7',  # reddish purple
    '#E69F00',  # orange
    '#56B4E9',  # sky blue
    '#F0E442',  # yellow
    '#000000',  # black
]
```

### Sequential / Continuous Data
- Use perceptually uniform colormaps: `viridis`, `magma`, `plasma`, `inferno`
- **NEVER** use `jet`, `rainbow`, `hsv`, or `spectral` colormaps

### Diverging Data
- Use `coolwarm` or `RdBu_r` (red-blue diverging)

---

## Colorblind Accessibility (MANDATORY)

Color alone is never sufficient to distinguish data series. Always combine color with at least one of:
- **Bar charts**: Add hatching patterns (`/`, `\\`, `x`, `.`, `o`)
- **Line charts**: Use distinct line styles (solid, dashed, dotted, dash-dot) AND geometric markers
- **Scatter plots**: Use different marker shapes (circle, square, triangle, diamond, plus)

---

## Typography

- **Font family**: Sans-serif exclusively (Helvetica, Arial, DejaVu Sans)
- **Axis labels**: Same size as template body text (read from `figure_config.json`)
- **Tick labels**: 1pt smaller than axis labels
- **Title**: Same size as axis labels (or omit -- LaTeX caption is preferred)
- **Legend text**: 1pt smaller than axis labels
- **All text must be readable when printed** at column width (minimum 8pt equivalent)

---

## Chart-Type Specific Rules

### Bar Charts
- Bars with thin black outlines (0.5pt) OR completely borderless
- Error bars: black, with flat caps (capsize=3)
- Group spacing: 0.8 width ratio, 0.2 gap
- For many bars (>6): consider horizontal bars to avoid rotated labels
- Y-axis should start at 0 unless there's a strong reason not to

### Line Charts
- Always include geometric markers at actual data points (circle `o`, square `s`, triangle `^`, diamond `D`)
- Primary/measured data: solid lines (1.5-2pt)
- Secondary/theoretical/baseline: dashed or dotted lines
- Confidence intervals: shaded bands (alpha=0.2-0.3)
- Line width: 1.2-2.0pt

### Scatter Plots
- Different marker shapes encode categorical dimensions (not just color)
- Fully opaque fills (alpha=1.0) for small datasets
- Semi-transparent (alpha=0.5-0.7) only when points overlap significantly
- Marker size: 4-8pt

### Heatmaps
- Cells must be square
- Annotate values inside cells (white text on dark, black text on light)
- Use thin white separators (0.5pt) or borderless
- Colorbar always present with label

### Box Plots / Violin Plots
- Show individual data points overlaid (jittered strip, alpha=0.4, size=3)
- Median line clearly visible (black, 2pt)
- Use same colorblind-safe palette as other charts
- Violin: add inner mini box plot for quartiles

### Grouped Bar Charts
- Group bars with `width = 0.8 / n_groups`, offset by `x + i*width`
- Each group gets a distinct color from Wong palette
- Add hatching patterns for colorblind differentiation
- Group labels centered below the group
- Individual bar labels rotated if needed or use horizontal bars

### Stacked Bar Charts
- Use sequential lightness of same hue (e.g., light blue to dark blue)
- Add thin white borders (1pt) between stacked segments
- Annotate segment values inside (white text on dark, black on light)
- Legend order matches visual stack order (bottom to top)

### Dual-Axis Charts
- Left axis: bars or primary data (standard color)
- Right axis: line with markers (contrasting color, e.g., red)
- Clearly label both axes with units and matching colors
- Use `ax.twinx()` — never plot both on same axis
- Include legend that labels both series

### Radar / Spider Charts
- Fill with semi-transparent color (alpha=0.2)
- Bold outline (1.5pt) with markers at each vertex
- Label each axis at the outer ring
- If comparing multiple series: distinct colors + different line styles

### Donut Charts (instead of pie)
- Use `wedgeprops=dict(width=0.4)` for donut hole
- Annotate percentages outside with leader lines
- Sort slices largest to smallest (clockwise)
- Maximum 6-7 slices; group small ones into "Other"

### Confusion Matrix
- Use `imshow` with diverging colormap (`RdYlGn` or `coolwarm`)
- Annotate every cell with the value (white on dark, black on light)
- Square cells: `ax.set_aspect('equal')`
- Labels on both axes, rotated x-labels if needed

### Error Bar Charts
- Caps: `capsize=3`, black color
- Error bar linewidth: 1pt, same color as bar edge
- If asymmetric errors: use `yerr=[[lower], [upper]]`

### Multi-Panel Figures (subplots)
- Use `fig, axes = plt.subplots(1, n, figsize=(...), constrained_layout=True)`
- Share y-axis when comparing same metric: `sharey=True`
- Remove redundant y-labels on inner panels
- Panel labels: **(a)**, **(b)**, **(c)** as bold text, top-left of each panel
- Consistent axis ranges across panels when possible

---

## Matplotlib Code Patterns (for writer agent reference)

### Standard figure setup
```python
import json
import matplotlib.pyplot as plt

# Load figure config
with open('paper/figures/figure_config.json') as f:
    cfg = json.load(f)
plt.rcParams.update(cfg['matplotlib_rcparams'])
W = cfg['geometry']['columnwidth_in']

# Wong colorblind-safe palette
COLORS = ['#0072B2', '#D55E00', '#009E73', '#CC79A7', '#E69F00', '#56B4E9', '#F0E442']
HATCHES = ['', '///', '\\\\\\', '...', 'xxx', '+++', 'ooo']
```

### Horizontal bar chart (avoids x-label overlap)
```python
fig, ax = plt.subplots(figsize=(W, W*0.5), constrained_layout=True)
y = range(len(labels))
bars = ax.barh(y, values, color=colors, edgecolor='#333', linewidth=0.5)
for bar, h in zip(bars, hatches): bar.set_hatch(h)
ax.set_yticks(y); ax.set_yticklabels(labels)
ax.invert_yaxis()
ax.spines['top'].set_visible(False); ax.spines['right'].set_visible(False)
```

### Multi-panel with shared axis
```python
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(W, W*0.45), sharey=True, constrained_layout=True)
ax1.set_title('(a) Metric A', fontweight='bold')
ax2.set_title('(b) Metric B', fontweight='bold')
ax2.set_yticklabels([])  # remove duplicate y labels
```

---

## Legend Placement

- **Inside plot area**: upper-right or lower-right, with semi-transparent background (framealpha=0.8)
- **Outside plot area**: horizontal above the plot or to the right
- **Never** let the legend obstruct data points
- For many series (>6): place legend outside plot area

---

## Figure Sizing

- **Always** read `figure_config.json` for exact dimensions
- Single-column: `figsize = (columnwidth_in, columnwidth_in * 0.7)`
- Full-width: `figsize = (textwidth_in, textwidth_in * 0.35)` or taller if needed
- Use `constrained_layout=True` or `tight_layout(pad=1.5)` to prevent label clipping
- DPI: 300 for final output

---

## Axis Configuration

- Label all axes with descriptive names and units (e.g., "Latency (ms)")
- No truncated axes unless explicitly justified (and if so, add axis break marks)
- Use scientific notation for very large/small numbers
- Rotate x-tick labels only as last resort (prefer shorter labels or horizontal bars)
- Remove unnecessary tick marks

---

## Figure Placement in Multi-Column LaTeX Templates

Choose the correct LaTeX environment based on figure complexity:

| Figure Type | Environment | Width | When |
|-------------|------------|-------|------|
| Simple single chart | `\begin{figure}` | `\columnwidth` | One bar/line/scatter plot, few labels |
| Complex/dense chart | `\begin{figure*}` | `\textwidth` | Many bars, dense annotations, dual-axis |
| Multi-panel (a)(b) | `\begin{figure*}` | `\textwidth` | Side-by-side subplots |
| Concept/architecture | `\begin{figure*}` | `\textwidth` | Pipeline, system overview, flowchart |

When generating matplotlib figures:
- Single-column: `figsize=(columnwidth_in, columnwidth_in * 0.7)`
- Full-width: `figsize=(textwidth_in, textwidth_in * 0.35)` or taller if needed
- Read both values from `figure_config.json`

---

## Anti-Patterns (NEVER do these)

- No 3D effects (no 3D bar charts, no 3D scatter unless truly 3D data)
- No pie charts (use horizontal bar charts or donut charts instead)
- No `jet` / `rainbow` / `hsv` colormaps
- No serif fonts in plots
- No solid black grid lines
- No color-only differentiation (always add shape/pattern/style)
- No chartjunk (decorative borders, unnecessary graphics, watermarks)
- No rotated x-labels if avoidable (use horizontal bars instead)
- No figure titles inside the plot (use LaTeX `\caption{}`)
- No excessive tick marks or minor ticks without purpose
- No legend that overlaps with data
- No default matplotlib styling (always set rcParams from figure_config.json)
