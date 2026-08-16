"""refresh_model_versions.apply_to_text: advance every picker chip + its label.

Regression guard for the 2026-07-19 bug where only the create picker's value
was patched — continue/restart kept the stale slug (wrong model on restart)
and every display label lagged the value (Kimi K3 slug labeled 'Kimi K2.6').
"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "refresh_model_versions", ROOT / "scripts" / "refresh_model_versions.py")
rmv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rmv)


def _chip(picker, slug, label):
    return (f'<input type="radio" name="{picker}" value="openrouter/{slug}" />\n'
            f'<span class="model-chip">{label} '
            f'<span class="model-meta"><span class="cost">$$</span></span></span>')


def test_all_three_pickers_and_labels_advance():
    text = "\n".join([
        _chip("model", "moonshotai/kimi-k2.6", "Kimi K2.6"),
        _chip("continue-model", "moonshotai/kimi-k2.6", "Kimi K2.6"),
        _chip("restart-model", "moonshotai/kimi-k2.6", "Kimi K2.6"),
    ])
    out, changes = rmv.apply_to_text(text, {"kimi": "moonshotai/kimi-k3"})
    assert out.count("openrouter/moonshotai/kimi-k3") == 3
    assert "kimi-k2.6" not in out
    assert out.count(">Kimi K3 ") == 3
    assert "Kimi K2.6" not in out
    assert changes and "3 picker chip(s)" in changes[0]


def test_label_phrasings_all_updated():
    text = "\n".join([
        _chip("model", "anthropic/claude-sonnet-4.6", "Claude Sonnet 4.6"),
        _chip("restart-model", "anthropic/claude-sonnet-4.6", "Sonnet 4.6"),
    ])
    out, _ = rmv.apply_to_text(text, {"claude-sonnet": "anthropic/claude-sonnet-5"})
    assert ">Claude Sonnet 5 " in out and ">Sonnet 5 " in out
    assert "4.6" not in out


def test_native_dash_format_untouched():
    # The direct-vendor default (dash format, no openrouter/ prefix) is not
    # catalog-tracked and must never be rewritten.
    text = ('<input type="radio" name="model" value="claude-sonnet-4-6" />\n'
            '<span class="model-chip">Sonnet 4.6 <span class="model-meta"></span></span>')
    out, changes = rmv.apply_to_text(text, {"claude-sonnet": "anthropic/claude-sonnet-5"})
    assert out == text and not changes


def test_no_change_when_current():
    text = _chip("model", "moonshotai/kimi-k3", "Kimi K3")
    out, changes = rmv.apply_to_text(text, {"kimi": "moonshotai/kimi-k3"})
    assert out == text and not changes


def test_free_chips_are_never_rewritten():
    """The row-3 ':free' chips are curated by hand. They sit right after a tracked
    chip in the real picker, so the refresh must neither adopt them as a family
    version nor smear a version token into their label."""
    text = "\n".join([
        _chip("model", "google/gemini-3.6-flash", "Gemini 3.6 Flash"),
        _chip("model", "google/gemma-4-31b-it:free", "Gemma 4 31B"),
        _chip("model", "nvidia/nemotron-3.5-lightning:free", "Nemotron 3.5 Lightning"),
    ])
    out, _ = rmv.apply_to_text(text, {"gemini-flash": "google/gemini-4-flash"})
    assert "openrouter/google/gemma-4-31b-it:free" in out
    assert "openrouter/nvidia/nemotron-3.5-lightning:free" in out
    assert ">Gemma 4 31B " in out and ">Nemotron 3.5 Lightning " in out
    assert "openrouter/google/gemini-4-flash" in out   # the paid chip did advance


def test_free_variants_are_not_picked_as_a_family_latest():
    """A ':free' catalog id must never win latest_per_family and be promoted into
    a paid chip (the TRACKED patterns are end-anchored — this guards that)."""
    latest = rmv.latest_per_family({"google/gemini-3.6-flash", "google/gemini-9.9-flash:free"})
    assert latest["gemini-flash"] == "google/gemini-3.6-flash"


def test_version_token():
    assert rmv._version_token("moonshotai/kimi-k3") == "3"
    assert rmv._version_token("anthropic/claude-sonnet-5") == "5"
    assert rmv._version_token("z-ai/glm-5.2") == "5.2"
