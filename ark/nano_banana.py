"""Nano Banana: Gemini-powered AI figure generation for scientific papers.

Uses Google's Gemini image generation models (Nano Banana Flash / Pro)
to create concept diagrams, architecture figures, and mechanism illustrations.

Models:
  - flash: gemini-3.1-flash-image-preview  (fast, free 500/day)
  - pro:   gemini-3-pro-image-preview       (highest quality, $0.13/img)
"""

import json
import os
import subprocess
import yaml
from pathlib import Path
from typing import Optional


# ══════════════════════════════════════════════════════════════
#  Models
# ══════════════════════════════════════════════════════════════

MODELS = {
    "flash": "gemini-3.1-flash-image-preview",
    "pro": "gemini-3-pro-image-preview",
}
DEFAULT_MODEL = "flash"


# ══════════════════════════════════════════════════════════════
#  API Key
# ══════════════════════════════════════════════════════════════

def get_api_key() -> str:
    """Get Gemini API key (reuses deep_research key)."""
    from ark.deep_research import get_gemini_api_key
    return get_gemini_api_key()


# ══════════════════════════════════════════════════════════════
#  Image Generation
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

    model_id = MODELS.get(model, MODELS[DEFAULT_MODEL])

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
#  Prompt Generation (Claude-assisted)
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
