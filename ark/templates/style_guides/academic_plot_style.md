# Academic Plot Style Guide

## Aesthetic: "Precision, Accessibility, High Contrast"

Academic statistical plots should prioritize data clarity over decoration. White backgrounds, readable fonts, and colorblind-safe palettes are mandatory.

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
- Show individual data points overlaid (jittered strip)
- Median line clearly visible
- Use same colorblind-safe palette as other charts

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
