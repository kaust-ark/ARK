"""The OpenRouter free-tier row is present, labelled, and never the default.

The picker is hand-duplicated three times (create / continue / restart) and has
drifted before, so the free chips are asserted in all three. They must also stay
BELOW the paid chips: selectDefaultModel() checks the first enabled radio, and a
rate-limited free model as the silent default would stall real runs.
"""

import re
from pathlib import Path

import pytest

APP_HTML = Path(__file__).resolve().parents[2] / "website" / "dashboard" / "templates" / "app.html"

PICKERS = ("model", "continue-model", "restart-model")

FREE_SLUGS = (
    "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
    "openrouter/nvidia/nemotron-3.5-lightning:free",
    "openrouter/google/gemma-4-31b-it:free",
    "openrouter/poolside/laguna-s-2.1:free",
)


@pytest.fixture(scope="module")
def html():
    return APP_HTML.read_text()


@pytest.mark.parametrize("picker", PICKERS)
@pytest.mark.parametrize("slug", FREE_SLUGS)
def test_free_model_in_every_picker(html, picker, slug):
    assert f'name="{picker}" value="{slug}"' in html


@pytest.mark.parametrize("picker", PICKERS)
def test_free_chips_are_marked_free_and_rate_limited(html, picker):
    """Free is only half the story — the daily cap has to be visible too, or a
    user meets it as a stalled run."""
    for slug in FREE_SLUGS:
        chip = re.search(
            r'name="' + re.escape(picker) + r'" value="' + re.escape(slug) + r'"'
            r'[\s\S]*?</label>', html)
        assert chip, f"{slug} chip missing from the {picker} picker"
        assert "🆓" in chip.group(0)
        assert "Rate-limited" in chip.group(0)


def test_rate_limit_note_shown_once_per_picker(html):
    assert html.count("OpenRouter caps free requests per day, so a long run can stall") == len(PICKERS)


@pytest.mark.parametrize("picker", PICKERS)
def test_free_model_is_not_the_default(html, picker):
    """First radio in DOM order is what selectDefaultModel() lands on."""
    first = re.search(r'name="' + re.escape(picker) + r'" value="([^"]+)"', html)
    assert first and not first.group(1).endswith(":free")
