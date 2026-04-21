"""Nano Banana: Gemini-powered AI figure generation for scientific papers.

Uses Google's Gemini image generation models (Nano Banana Flash / Pro)
to create concept diagrams, architecture figures, and mechanism illustrations.

Supports two modes:
  - One-shot: generate_figure() — single image generation call (legacy)
  - Pipeline: generate_figure_pipeline() — Planner → Stylist → Visualizer → Critic loop

Models:
  - flash: gemini-3.1-flash-image-preview  (fast, image generation)
  - pro:   gemini-3-pro-image-preview       (highest quality, image generation)
"""

import json
import os
import re
import subprocess
import yaml
from pathlib import Path
from typing import Optional


# ══════════════════════════════════════════════════════════════
#  Models
# ══════════════════════════════════════════════════════════════

IMAGE_MODELS = {
    "flash": "gemini-3.1-flash-image-preview",
    "pro": "gemini-3-pro-image-preview",
}

TEXT_MODELS = {
    "flash": "gemini-flash-latest",
    "pro": "gemini-pro-latest",
}

# Legacy alias
MODELS = IMAGE_MODELS
DEFAULT_MODEL = "flash"


# ══════════════════════════════════════════════════════════════
#  API Key
# ══════════════════════════════════════════════════════════════

def get_api_key() -> str:
    """Get Gemini API key (reuses deep_research key)."""
    from ark.deep_research import get_gemini_api_key
    return get_gemini_api_key()


# ══════════════════════════════════════════════════════════════
#  Style Guide Loader
# ══════════════════════════════════════════════════════════════

def _load_style_guide(guide_name: str) -> str:
    """Load a style guide from ark/templates/style_guides/.

    Args:
        guide_name: Name without extension (e.g. "academic_diagram_style")

    Returns:
        Style guide content string, or empty string if not found.
    """
    guide_path = Path(__file__).parent / "templates" / "style_guides" / f"{guide_name}.md"
    if guide_path.exists():
        return guide_path.read_text()
    return ""


# ══════════════════════════════════════════════════════════════
#  Gemini Text Helper
# ══════════════════════════════════════════════════════════════

def _call_gemini_text(client, model_id: str, prompt: str, image_bytes: bytes = None) -> str:
    """Call Gemini text model, optionally with an image for multimodal input.

    Args:
        client: google.genai.Client instance
        model_id: Text model ID (e.g. "gemini-flash-latest")
        prompt: Text prompt
        image_bytes: Optional PNG image bytes for multimodal (Critic) calls

    Returns:
        Response text string, or empty string on failure.
    """
    from google.genai import types

    try:
        if image_bytes:
            contents = [
                types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                prompt,
            ]
        else:
            contents = prompt

        response = client.models.generate_content(
            model=model_id,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["TEXT"],
            ),
        )
        return response.text or ""
    except Exception as e:
        print(f"Gemini text call error ({model_id}): {e}")
        return ""


# ══════════════════════════════════════════════════════════════
#  Image Generation (legacy, unchanged)
# ══════════════════════════════════════════════════════════════

def generate_figure(
    prompt: str,
    output_path: Path,
    api_key: str = None,
    model: str = DEFAULT_MODEL,
) -> bool:
    """Generate a single figure using Nano Banana.

    Args:
        prompt: Detailed image generation prompt
        output_path: Where to save the PNG
        api_key: Gemini API key (auto-detected if None)
        model: "flash" or "pro"

    Returns:
        True if image was generated and saved successfully.
    """
    key = api_key or get_api_key()
    if not key:
        return False

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("Error: google-genai package not installed. Run: pip install google-genai")
        return False

    model_id = IMAGE_MODELS.get(model, IMAGE_MODELS[DEFAULT_MODEL])

    try:
        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=model_id,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
            ),
        )

        for part in response.parts:
            if part.inline_data is not None:
                output_path = Path(output_path)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                image = part.as_image()
                image.save(str(output_path))
                return True

        return False
    except Exception as e:
        print(f"Nano Banana error: {e}")
        return False


def generate_concept_figures(
    figures: list,
    figures_dir: Path,
    api_key: str = None,
    model: str = DEFAULT_MODEL,
    log_fn=None,
) -> list:
    """Batch generate concept figures from a list of descriptions.

    Args:
        figures: List of dicts with keys: name (str), prompt (str)
        figures_dir: Directory to save generated PNGs
        api_key: Gemini API key
        model: "flash" or "pro"
        log_fn: Optional logging function

    Returns:
        List of successfully generated file paths.
    """
    key = api_key or get_api_key()
    if not key:
        if log_fn:
            log_fn("No Gemini API key found, skipping AI figure generation", "WARN")
        return []

    generated = []
    figures_dir = Path(figures_dir)
    figures_dir.mkdir(parents=True, exist_ok=True)

    for fig in figures:
        name = fig.get("name", "figure")
        prompt = fig.get("prompt", "")
        if not prompt:
            continue

        output_path = figures_dir / f"{name}.png"
        if log_fn:
            log_fn(f"Generating figure: {name}...", "INFO")

        ok = generate_figure(prompt, output_path, api_key=key, model=model)
        if ok:
            if log_fn:
                log_fn(f"  Generated: {output_path.name}", "INFO")
            generated.append(str(output_path))
        else:
            if log_fn:
                log_fn(f"  Failed to generate: {name}", "WARN")

    return generated


# ══════════════════════════════════════════════════════════════
#  Prompt Generation (legacy, unchanged)
# ══════════════════════════════════════════════════════════════

def build_figure_prompt(
    figure_name: str,
    caption: str,
    paper_context: str,
    venue: str = "",
    column_width_in: float = 3.333,
) -> str:
    """Build a detailed image generation prompt for a scientific figure.

    Uses the paper context and caption to create a prompt that produces
    a clean, publication-ready concept diagram.
    """
    return f"""Create a professional scientific figure for an academic paper.

Figure: {figure_name}
Caption: {caption}

Context from the paper:
{paper_context[:2000]}

Style requirements:
- Clean white background, suitable for {venue or 'academic'} conference
- Professional scientific illustration style
- Clear, readable labels and annotations (minimum 10pt equivalent)
- No decorative elements, focus on clarity and accuracy
- Proportions suitable for {column_width_in:.1f} inch column width
- Use a consistent color palette (blues, greens, oranges for different components)
- Vector-like clean edges and shapes
- Include a clear legend if multiple elements are present

Generate a high-quality scientific concept diagram that would be suitable
for a top-tier academic publication."""


# ══════════════════════════════════════════════════════════════
#  Pipeline: Planner → Stylist → Visualizer → Critic
# ══════════════════════════════════════════════════════════════

def _run_planner(client, text_model_id: str, figure_name: str, caption: str,
                 paper_context: str, venue: str, column_width_in: float) -> str:
    """Plan a detailed visual description for a concept diagram.

    Returns a comprehensive textual specification of what the figure should contain,
    including layout, colors, shapes, arrows, and text labels.
    """
    prompt = f"""You are a world-class scientific illustration designer. Your task is to create a detailed visual specification for a concept diagram that will be rendered by an AI image generation model. The output should look like a **modern tech illustration** (think Figma or professional design tool output), NOT a basic flowchart or matplotlib diagram.

## Figure Information
- Figure name: {figure_name}
- Caption: {caption}
- Target venue: {venue or 'top-tier academic conference (NeurIPS/ICML level)'}
- Target width: {column_width_in:.1f} inches, landscape aspect ratio (~16:10)

## Paper Context
{paper_context[:3000]}

## Your Task

Design a PUBLICATION-QUALITY illustration. For each element, be extremely specific:

### 1. Composition & Layout
- Flow direction (left-to-right preferred for pipelines, top-to-bottom for hierarchies)
- How to group related components into background zones
- Visual hierarchy: what's the MAIN path the reader's eye should follow?
- Where to place secondary/error/feedback paths (de-emphasized)

### 2. Components (for EACH module/component)
- Shape: rounded rectangle (12px radius), cylinder (for storage only), diamond (for decisions)
- Fill color: assign HEX codes based on semantic role:
  * Input/data source → #90CAF9 (blue) on #E6F3FF zone
  * Processing/transformation → #80CBC4 (teal) on #E0F2F1 zone
  * Decision/classification → #FFB74D (orange) on #FFF3E0 zone
  * Output/result → #CE93D8 (purple) on #F3E5F5 zone
  * Error/rejection → #EF9A9A (red) on #FFEBEE zone
  * Storage/memory → #B0BEC5 (grey) on #ECEFF1 zone
- Border: 1px solid, slightly darker than fill (e.g., fill #90CAF9 → border #42A5F5)
- Text label: bold sans-serif, 12-14pt
- Icon suggestion: small relevant icon inside (shield for security, gear for processing, magnifying glass for detection, lock for access control, brain for AI, etc.)

### 3. Connections
- Main forward flow: solid dark grey (#424242) lines, 1.5px, orthogonal (right-angle) routing
- Secondary/auxiliary paths: dashed lines, lighter grey (#9E9E9E), 1px
- Error/rejection paths: dashed red (#EF5350) lines, 1px
- Arrow labels: regular sans-serif, 9pt, placed along the line
- Arrowheads: filled, proportional to line width

### 4. Background Zones
- Soft pastel backgrounds at 10-15% opacity to group related stages
- Generous padding (20px+) between components and zone edges
- Zone labels: italic sans-serif, 8-9pt, top-left corner of zone
- Zones should have rounded corners and soft edges

### 5. Visual Richness & Density
- The diagram should feel RICH and INFORMATIVE — not empty or sparse
- Add small illustrative icons/thumbnails inside key components (2D vector-style, NOT emoji)
- Use different shapes for different component types (not all rounded rectangles)
- Overall background: white or off-white (#FAFAFA)
- NO drop shadows, NO gradients, NO 3D effects. Flat design with semantic richness.
- Every visual element should encode meaning — if a color/shape/line doesn't convey information, remove it

### 6. TEXT vs VISUAL BALANCE (CRITICAL)
**Text must be SHORT, but visuals must be RICH.** These are different things.

TEXT rules (keep it minimal):
- Component labels: MAX 3-5 words. NO sentences inside components.
- Connection labels: MAX 1-3 words (e.g., "Benign", "Repaired")
- NO paragraphs, bullet lists, or multi-line text inside components
- Total visible text in the figure: under ~60 words

VISUAL rules (make it rich and detailed):
- Each component should have a DETAILED, recognizable icon (not a simple flat shape — e.g., a magnifying glass hovering over a document for detection, gears with a wrench for repair, a shield with embedded lock for security, a brick wall with a scanning beam for firewall)
- Show internal sub-structure visually: nested mini-elements, small thumbnails, overlapping shapes
- Use visual metaphors: a funnel for filtering, a pipeline for flow, stacked layers for hierarchy

LAYOUT rules (keep it compact):
- MINIMIZE whitespace between components. Pack elements closely.
- Components should feel tightly arranged with clear, short connections
- Avoid large empty areas — if there's space, add a visual annotation or detail
- The diagram should feel DENSE and INFORMATIVE even without reading the text

IMPORTANT: Do NOT include font sizes (e.g., "12pt"), hex color codes (e.g., "#E6F3FF"), or CSS-like properties in component descriptions. Those are for the style guide only. Just describe WHAT to draw — shapes, labels, connections, zones, icons — in plain language. The image generator will interpret font specs as literal text to render.

Output ONLY the visual specification. No preamble."""

    return _call_gemini_text(client, text_model_id, prompt)


def _run_stylist(client, text_model_id: str, description: str, style_guide: str) -> str:
    """Refine a visual description to match academic publication aesthetics.

    Preserves all semantic content while improving visual styling.
    """
    prompt = f"""You are a senior art director for top-tier AI conferences (NeurIPS, ICML, ICLR). Your task is to elevate a figure description from "functional diagram" to "publication-quality illustration."

## RULES
- PRESERVE all semantic content, logic, structure, and components
- ELEVATE the visual quality — make it look like professional design, not a basic flowchart
- ENFORCE the style guide strictly (HEX colors, typography hierarchy, icon suggestions)

## Style Guide
{style_guide}

## Figure Description to Refine
{description}

## Your Refinement Checklist

1. **Colors**: Verify every component has a HEX code matching its semantic role (input→blue, processing→green, etc.). Replace vague colors ("light blue") with exact HEX.

2. **Typography hierarchy**: Ensure exactly 3 levels:
   - Component names: bold sans-serif 12-14pt
   - Connection labels: regular sans-serif 9-10pt
   - Zone labels / annotations: light/italic 8-9pt

3. **Visual depth**: Add these if missing:
   - Subtle shadows (2px, 8% opacity) under main components
   - 12px rounded corners on all rectangular elements
   - Thin border strokes (1px, 15-20% darker than fill)
   - Soft edges on background zones

4. **Icons**: Suggest a small icon for each major component (shield, gear, magnifying glass, lock, brain, document, etc.)

5. **Composition**: Ensure the main flow path is visually dominant. Secondary paths (error, feedback) should be thinner, lighter, dashed.

6. **Anti-patterns**: Remove any flowchart-like flat styling. The output must feel like a Figma illustration, not PowerPoint.

Output ONLY the refined description. No preamble."""

    return _call_gemini_text(client, text_model_id, prompt)


def _run_critic(client, text_model_id: str, description: str, image_bytes: bytes,
                caption: str, style_guide: str) -> dict:
    """Evaluate a generated figure against its description and style guide.

    Returns a dict with scores and suggestions. Uses multimodal input
    (image + text) to visually assess the generated figure.
    """
    prompt = f"""You are an elite visual design critic for NeurIPS/ICML papers. You have extremely high standards. A score of 5 means "this could appear in a best paper award submission." You almost never give 5.

Examine the attached figure critically.

## Figure Caption
{caption}

## Visual Description (what was requested)
{description[:3000]}

## Style Guide Reference
{style_guide[:2000]}

## AUTOMATIC FAILURES (check FIRST — any match caps the score)

- Figure title/heading inside image → Aesthetics ≤ 3
- Text overlap or occlusion → Readability ≤ 2
- Missing described components → Faithfulness ≤ 3
- Looks like a basic flowchart (generic boxes + arrows, no visual richness) → Publication Readiness ≤ 2
- Only 1-2 colors / monotone → Publication Readiness ≤ 2
- Heavy black outlines or PowerPoint defaults → Publication Readiness ≤ 2
- No semantic color coding (colors are random, don't encode meaning) → Publication Readiness ≤ 3
- All text same size (no typography hierarchy) → Publication Readiness ≤ 3
- Sparse/empty layout with too much white space → Publication Readiness ≤ 3
- Drop shadows or 3D effects (should be FLAT design) → Aesthetics ≤ 3
- Text overload: sentences, paragraphs, or bullet lists inside components → Conciseness ≤ 2
- Component labels longer than 5 words → Conciseness ≤ 3
- Total visible text exceeds ~50 words → Conciseness ≤ 3

## Scoring Criteria (1-5, be VERY strict)

1. **Faithfulness** (1-5): All described components present? Connections correct? Labels accurate?

2. **Conciseness** (1-5): No visual clutter? No unnecessary text? Every element serves a purpose?

3. **Readability** (1-5): Clear flow? Labels readable at print size? No overlap? Good spacing?

4. **Aesthetics** (1-5): Professional color palette? Semantic color coding? Consistent styling?

5. **Publication Readiness** (1-5): THE MOST IMPORTANT CRITERION.
   - Does this look like a figure from a NeurIPS/ICML best paper?
   - Is it RICH and INFORMATIVE — densely packed with semantic elements but still clear?
   - Does it use semantic color coding (colors encode meaning, not decoration)?
   - Does it use different shapes for different component types?
   - Does it have typography hierarchy (bold for components, regular for labels, italic for math)?
   - Does it have small icons/illustrations inside components to reinforce meaning?
   - Is it flat design (NO shadows, NO 3D, NO gradients)?
   - 1 = looks like matplotlib/PowerPoint. 3 = clean but generic. 5 = NeurIPS best paper quality.

## Output (STRICT JSON, nothing else)

```json
{{
  "faithfulness": <1-5>,
  "conciseness": <1-5>,
  "readability": <1-5>,
  "aesthetics": <1-5>,
  "publication_readiness": <1-5>,
  "overall": <1-5>,
  "critic_suggestions": "<specific improvements, or 'No changes needed.'>",
  "revised_description": "<COMPLETE revised visual description if changes needed, or empty string>"
}}
```

IMPORTANT: If publication_readiness ≤ 3, you MUST provide a revised_description that adds specific visual polish instructions (shadows, icons, typography hierarchy, semantic colors)."""

    text = _call_gemini_text(client, text_model_id, prompt, image_bytes=image_bytes)
    if not text:
        return {"faithfulness": 3, "conciseness": 3, "readability": 3, "aesthetics": 3,
                "overall": 3, "critic_suggestions": "", "revised_description": ""}

    # Parse JSON from response (may have markdown fences)
    try:
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            result = json.loads(json_match.group())
            # Ensure required keys
            for key in ("faithfulness", "conciseness", "readability", "aesthetics",
                        "publication_readiness", "overall", "critic_suggestions", "revised_description"):
                if key not in result:
                    result[key] = 3 if key not in ("critic_suggestions", "revised_description") else ""
            return result
    except (json.JSONDecodeError, AttributeError):
        pass

    return {"faithfulness": 3, "conciseness": 3, "readability": 3, "aesthetics": 3,
            "overall": 3, "critic_suggestions": "", "revised_description": ""}


# ══════════════════════════════════════════════════════════════
#  Pipeline Entry Point
# ══════════════════════════════════════════════════════════════

def generate_figure_pipeline(
    figure_name: str,
    caption: str,
    paper_context: str,
    output_path: Path,
    api_key: str = None,
    model: str = DEFAULT_MODEL,
    venue: str = "",
    column_width_in: float = 3.333,
    max_critic_rounds: int = 3,
    log_fn=None,
) -> bool:
    """Generate a concept figure using the full Planner → Stylist → Critic pipeline.

    This is the recommended entry point for high-quality figure generation.
    Uses Gemini text models for planning/styling/critiquing and Gemini image
    models for actual generation.

    Args:
        figure_name: Short name for the figure (e.g. "fig_overview")
        caption: Figure caption from LaTeX
        paper_context: Relevant text from the paper (method section, etc.)
        output_path: Where to save the final PNG
        api_key: Gemini API key (auto-detected if None)
        model: "flash" or "pro" (controls both text and image model tier)
        venue: Target venue name (e.g. "NeurIPS", "ICML")
        column_width_in: Target figure width in inches
        max_critic_rounds: Maximum Critic feedback loops (default 3)
        log_fn: Optional logging function(msg, level)

    Returns:
        True if figure was generated successfully.
    """
    def _log(msg, level="INFO"):
        if log_fn:
            log_fn(msg, level)
        else:
            print(f"[{level}] {msg}")

    key = api_key or get_api_key()
    if not key:
        _log("No Gemini API key found", "WARN")
        return False

    try:
        from google import genai
    except ImportError:
        _log("google-genai package not installed. Run: pip install google-genai", "ERROR")
        return False

    client = genai.Client(api_key=key)
    text_model = TEXT_MODELS.get(model, TEXT_MODELS[DEFAULT_MODEL])
    output_path = Path(output_path)

    # Load style guide
    style_guide = _load_style_guide("academic_diagram_style")

    # ── Step 1: Planner ──
    _log(f"[Planner] Creating visual spec for {figure_name}...")
    description = _run_planner(client, text_model, figure_name, caption,
                               paper_context, venue, column_width_in)
    if not description:
        _log("Planner failed to generate description, falling back to one-shot", "WARN")
        prompt = build_figure_prompt(figure_name, caption, paper_context, venue, column_width_in)
        return generate_figure(prompt, output_path, api_key=key, model=model)

    # ── Step 2: Stylist ──
    _log(f"[Stylist] Refining visual spec with style guide...")
    styled_description = _run_stylist(client, text_model, description, style_guide)
    if not styled_description:
        _log("Stylist failed, using Planner output directly", "WARN")
        styled_description = description

    # ── Step 3: Visualizer → Critic Loop ──
    best_path = None
    for round_idx in range(max_critic_rounds):
        # Build image generation prompt
        image_prompt = f"""Generate a publication-quality scientific methodology diagram for a top-tier AI conference paper (NeurIPS/ICML level).

{styled_description}

MANDATORY VISUAL STYLE — "Soft Tech & Scientific Pastels":
- Aesthetic: approachable yet precise. Think NeurIPS 2024-2025 best paper figures.
- Background zones: use VERY LIGHT desaturated pastels (10-15% opacity) — cream #F5F5DC, pale blue #E6F3FF, mint #E0F2F1, lavender #F3E5F5 — to group related components. NOT solid colored boxes.
- Component shapes: rounded rectangles (5-10px radius) for processes. Use different shapes for different types: cylinders for databases, diamonds for decisions, parallelograms for I/O.
- Fill colors: medium saturation pastels. Warm tones (salmon, peach, coral) for active/trainable components. Cool tones (sky blue, mint, lavender) for static/fixed components.
- Lines: orthogonal (right-angle) routing for architecture flows. Curved Bezier for feedback loops. Solid dark grey for forward flow, dashed lighter grey for auxiliary paths, dashed red for error paths.
- Typography: sans-serif BOLD for component names, sans-serif regular for connection labels, serif italic for any math notation. Clear size hierarchy.
- Small illustrative icons or thumbnails inside components to reinforce meaning — these should be simple, clean 2D vector-style icons (NOT emoji).
- The diagram should feel RICH and INFORMATIVE — densely packed with semantic elements but still organized and clear. Avoid empty space.
- Every visual element (color, shape, line style) should encode meaning, not just decoration.

ABSOLUTE PROHIBITIONS:
- NO figure title or heading text at the top of the image
- NO subtitle or description text — ONLY component labels and connection labels
- NO font specifications rendered as text (do NOT render "9pt", "Bold", "Sans-Serif", hex codes like "#424242" as visible text in the image)
- NO heavy black outlines or borders. Use thin, subtle strokes.
- NO 3D effects, gradients, or textures (unless encoding data dimensionality)
- NO drop shadows (flat design, not material design)
- Proportions: landscape, aspect ratio ~16:10, width ~{column_width_in:.1f} inches
- Background: white or off-white (#FAFAFA)"""

        # Generate image
        _log(f"[Visualizer] Generating image (round {round_idx + 1}/{max_critic_rounds})...")
        ok = generate_figure(image_prompt, output_path, api_key=key, model=model)
        if not ok:
            _log(f"Image generation failed in round {round_idx + 1}", "WARN")
            if best_path and best_path.exists():
                # Roll back to previous best
                import shutil
                shutil.copy2(best_path, output_path)
            break

        # Save as candidate for potential rollback
        best_path = output_path

        # Read generated image for Critic
        try:
            image_bytes = output_path.read_bytes()
        except Exception:
            _log("Failed to read generated image for critic", "WARN")
            break

        # Run Critic
        _log(f"[Critic] Evaluating (round {round_idx + 1}/{max_critic_rounds})...")
        eval_result = _run_critic(client, text_model, styled_description,
                                  image_bytes, caption, style_guide)

        f_score = eval_result.get("faithfulness", 0)
        c_score = eval_result.get("conciseness", 0)
        r_score = eval_result.get("readability", 0)
        a_score = eval_result.get("aesthetics", 0)
        p_score = eval_result.get("publication_readiness", 0)
        overall = eval_result.get("overall", 0)
        suggestions = eval_result.get("critic_suggestions", "")

        _log(f"  Scores: F={f_score} C={c_score} R={r_score} A={a_score} P={p_score} Overall={overall}")
        if suggestions and suggestions != "No changes needed.":
            _log(f"  Suggestions: {suggestions[:200]}")

        # Check if we're done — require BOTH overall >= 4 AND publication_readiness >= 4
        if suggestions == "No changes needed." or (overall >= 4 and p_score >= 4):
            _log(f"[Critic] Approved! (overall={overall}, pub_readiness={p_score})")
            break

        # Use revised description for next round
        revised = eval_result.get("revised_description", "")
        if revised and revised.strip():
            styled_description = revised
        elif suggestions and suggestions.strip():
            # Critic gave suggestions but no revised description — append suggestions
            # to the existing description so the next round can incorporate them
            styled_description = f"""{styled_description}

IMPORTANT CORRECTIONS (from quality review):
{suggestions}

Apply ALL the corrections above. Fix every issue mentioned."""
            _log("Critic gave suggestions without revised description, appending to spec", "INFO")
        else:
            _log("Critic gave no actionable feedback, stopping", "WARN")
            break

    success = output_path.exists() and output_path.stat().st_size > 0
    if success:
        _log(f"Pipeline complete: {output_path.name}")
    else:
        _log(f"Pipeline failed for {figure_name}", "WARN")
    return success


# ══════════════════════════════════════════════════════════════
#  CLI Entry Point (for standalone testing)
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Nano Banana: AI figure generation for academic papers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # One-shot generation
  python -m ark.nano_banana --prompt "Neural network architecture diagram" --output /tmp/test.png

  # Full pipeline (Planner → Stylist → Critic loop)
  python -m ark.nano_banana --pipeline --name "fig_overview" \\
    --caption "System architecture overview" \\
    --context "Our system uses a multi-agent pipeline..." \\
    --output /tmp/test_pipeline.png --model flash
""")
    parser.add_argument("--prompt", help="Direct prompt for one-shot generation")
    parser.add_argument("--pipeline", action="store_true", help="Use full Planner→Stylist→Critic pipeline")
    parser.add_argument("--name", default="test_figure", help="Figure name (pipeline mode)")
    parser.add_argument("--caption", default="", help="Figure caption (pipeline mode)")
    parser.add_argument("--context", default="", help="Paper context (pipeline mode)")
    parser.add_argument("--context-file", help="Read paper context from file")
    parser.add_argument("--output", default="nano_banana_output.png", help="Output PNG path")
    parser.add_argument("--model", default="flash", choices=["flash", "pro"])
    parser.add_argument("--max-rounds", type=int, default=3, help="Max critic rounds (pipeline mode)")
    parser.add_argument("--venue", default="", help="Target venue")
    parser.add_argument("--width", type=float, default=3.333, help="Column width in inches")
    args = parser.parse_args()

    # Read context from file if specified. Gemini 2.x handles large contexts
    # natively — we don't pre-truncate here. generate_figure_pipeline calls
    # Gemini, which does not have a Read tool, so the context must travel as
    # a string. If cost or latency becomes a problem, gate at call time with
    # an explicit, generous cap, not a silent 5000-char slice that drops the
    # second half of a finding.
    context = args.context
    if args.context_file:
        context = Path(args.context_file).read_text()

    def log_fn(msg, level="INFO"):
        print(f"[{level}] {msg}")

    if args.pipeline:
        ok = generate_figure_pipeline(
            figure_name=args.name,
            caption=args.caption or args.name,
            paper_context=context,
            output_path=Path(args.output),
            model=args.model,
            venue=args.venue,
            column_width_in=args.width,
            max_critic_rounds=args.max_rounds,
            log_fn=log_fn,
        )
    else:
        prompt = args.prompt or "A professional scientific concept diagram"
        ok = generate_figure(prompt, Path(args.output), model=args.model)

    print(f"\n{'Success' if ok else 'Failed'}: {args.output}")
