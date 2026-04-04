# Academic Diagram Style Guide

## Aesthetic: "Soft Tech & Scientific Pastels"

Modern academic diagrams use light-value backgrounds to organize complexity, reserving color saturation for the most critical active elements. The overall feel should be approachable yet precise.

---

## Color Palette

### Background Zones (10-15% opacity, desaturated pastels)
Use these to group related components into logical regions:
- Cream/Beige: `#F5F5DC` -- general grouping
- Pale Blue/Ice: `#E6F3FF` -- data flow or input regions
- Mint/Sage: `#E0F2F1` -- processing or transformation stages
- Pale Lavender: `#F3E5F5` -- output or evaluation regions
- Light Orange: `#FFF3E0` -- highlight or attention regions

### Functional Element Colors
- **Trainable/active components**: warm tones (red `#E57373`, orange `#FFB74D`, deep pink `#F06292`)
- **Frozen/static/fixed components**: cool tones (grey `#BDBDBD`, ice blue `#90CAF9`, cyan `#80DEEA`)
- **High saturation**: reserved exclusively for errors, loss signals, and final outputs
- **Text/outlines**: dark grey `#333333` or `#424242` (avoid pure black `#000000`)

### Maximum Colors
No more than 6 distinct hue families in a single diagram. Prefer 3-4.

---

## Shapes & Containers

- **Process nodes / modules**: Rounded rectangles (5-10px corner radius). This is the default shape (~80% of elements).
- **Tensors / data volumes**: 3D cuboids (isometric) to suggest dimensionality
- **Matrices / flat data**: Flat squares or rectangles
- **Databases / memory / storage**: Cylinders (use ONLY for actual storage)
- **Decisions / conditions**: Diamonds
- **Grouping / stages**: Solid rounded containers for global view; dashed borders for logical sub-stages
- **Inputs / outputs**: Parallelograms or rounded rectangles with distinct fill

### Sizing
- Consistent node sizes within each category
- Minimum readable size: labels must fit without overflow
- Proportional to conceptual importance (key modules slightly larger)

---

## Lines & Arrows

- **Architecture / forward flow**: Orthogonal (right-angle) routing, solid lines, dark grey or black
- **System logic / feedback loops**: Curved Bezier lines
- **Forward data flow**: Solid arrows (1.5-2px weight)
- **Gradient updates / backpropagation**: Dashed arrows
- **Skip connections / auxiliary paths**: Dotted arrows
- **Loss / objective signals**: Colored dashed arrows (red or orange)

### Arrow Tips
- Filled triangular arrowheads (not open)
- Consistent size across the diagram
- Place mathematical operators (sum, concat) on intersection points, not inline

### Routing
- Avoid crossing arrows where possible
- Use waypoints to route around obstacles
- Never let arrows overlap with text labels

---

## Typography

- **Component labels**: Sans-serif (Arial, Roboto, Helvetica). Bold for module/component names.
- **Annotations / descriptions**: Sans-serif, regular weight, slightly smaller
- **Mathematical variables**: Serif font, italicized (e.g., *x*, *z*, *L*)
- **Minimum font size**: 10pt equivalent (must be readable when printed at column width)
- **Maximum font levels**: Use no more than 3 distinct font sizes in one figure
- **Figure titles**: Do NOT include figure titles inside the image (LaTeX caption handles this)

---

## Layout Principles

- **Primary flow direction**: Left-to-right OR top-to-bottom. Be consistent within a figure.
- **Alignment**: Center-align nodes within rows/columns. Use a grid-based layout.
- **Spacing**: Consistent gaps between nodes (at least 1.5x the arrow line width)
- **Grouping**: Use background shading zones to cluster related components
- **White space**: Leave breathing room. Avoid cramming elements to fill space.
- **Aspect ratio**: Match the target column width (typically 3.3in for single-column, 7.0in for full-width). Height should be 50-80% of width.

---

## Iconography (optional)

Use sparingly and consistently:
- Trainable states: fire, lightning bolt
- Frozen states: snowflake, padlock
- Operations: gear, magnifying glass
- Content types: document icon, chat bubble, image thumbnail
- Avoid clip-art or overly decorative icons

---

## Domain-Specific Approaches

### LLM / Agent Papers
- Narrative, illustrative tone
- UI-style aesthetics (chat windows, tool panels)
- Cute/friendly icons (robot avatars, speech bubbles)
- Flow-chart style with clear sequential steps

### Computer Vision Papers
- Spatial, geometric layouts
- RGB color coding for channels
- Camera frustums, bounding boxes
- Grid-based feature map representations

### Theoretical / Mathematical Papers
- Minimalist design
- Grayscale with a single accent color
- Graph/node-based representations
- Emphasis on mathematical notation

### Systems / Infrastructure Papers
- Layered stack diagrams
- Clear separation between hardware/software layers
- Network topology visualizations
- Consistent left-to-right data flow

---

---

## Visual Quality: Illustration, Not Flowchart

The goal is a **modern tech illustration** — the kind you see in top NeurIPS/ICML papers. NOT a flowchart, NOT a PowerPoint slide, NOT matplotlib output.

### Required Visual Qualities
- **Flat design**: NO drop shadows, NO 3D effects, NO gradients. Sophistication through semantic richness, not decoration.
- **Rounded corners**: 5-10px radius on process nodes (consistent, not too round)
- **Soft pastel zones**: Background grouping zones at 10-15% opacity — barely visible, just enough to create visual grouping
- **Professional arrowheads**: Filled, proportional to line width, with clean orthogonal routing
- **Dense but organized**: The diagram should be RICH with information — use space efficiently, avoid large empty areas
- **Visual variety**: Use different shapes for different types (rounded rect for process, cylinder for storage, diamond for decision, parallelogram for I/O)

### Icons Inside Components (Recommended)
Add small, simple icons inside key components to reinforce meaning:
- **Security/defense**: Shield, lock, checkmark
- **Detection/search**: Magnifying glass, eye
- **Processing/compute**: Gear, CPU chip
- **Data/storage**: Database cylinder, document
- **Communication**: Speech bubble, envelope
- **AI/ML**: Brain, neural network node, robot
- **Error/warning**: Triangle exclamation, X mark

Icons should be simple line-art style, 20-24px, placed top-left or centered above the label.

### Color → Role Semantic Mapping

Colors must signal meaning, not just decoration:

| Component Role | Background Zone | Element Fill | Border |
|---------------|----------------|-------------|--------|
| Input / Data source | `#E6F3FF` (pale blue) | `#90CAF9` (light blue) | `#42A5F5` |
| Processing / Transformation | `#E0F2F1` (mint) | `#80CBC4` (teal) | `#26A69A` |
| Decision / Classification | `#FFF3E0` (light orange) | `#FFB74D` (orange) | `#FB8C00` |
| Output / Result | `#F3E5F5` (lavender) | `#CE93D8` (light purple) | `#AB47BC` |
| Error / Rejection | `#FFEBEE` (light red) | `#EF9A9A` (salmon) | `#EF5350` |
| Storage / Memory | `#ECEFF1` (light grey) | `#B0BEC5` (grey) | `#78909C` |
| Active / Highlighted | `#FFF8E1` (cream) | `#FFD54F` (amber) | `#FFC107` |

### Typography Hierarchy (Exactly 3 Levels)

| Level | Use | Font | Size | Weight |
|-------|-----|------|------|--------|
| 1 (Primary) | Component/module names | Sans-serif | 12-14pt | **Bold** |
| 2 (Secondary) | Connection labels, zone titles | Sans-serif | 9-10pt | Regular |
| 3 (Tertiary) | Annotations, descriptions | Sans-serif | 8-9pt | Light/Italic |

---

## Anti-Patterns (NEVER do these)

- No flat/utilitarian styling — always add depth and polish
- No 3D perspective effects (except cuboids for tensors)
- No decorative clip-art or stock imagery
- No serif fonts on component labels
- No inconsistent arrow styles within the same figure
- No saturated/bright backgrounds (keep backgrounds light)
- No figure titles inside the image
- No more than 6 color families
- No PowerPoint default styling
- No black backgrounds
- No text smaller than 8pt equivalent
- No flowchart-only aesthetic — aim for illustration quality
