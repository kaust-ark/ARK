"""New users must be able to pick a model their key actually covers.

From mid-September to mid-October three users who had saved only a DeepSeek
key tried to create a project 11 times and were refused every time: every
chip in the picker needs another vendor's key, the gating code read the
long-tail key map off `window` (a top-level `let` is never a window property,
so it read {}), and their own DeepSeek models showed "(no key)". The disabled
default chip stayed checked, FormData dropped it, and the server fell back to
a Claude model they had no key for. The browser-level behaviour was verified
with Playwright; these tests pin the pieces that can be checked without one.
"""
from pathlib import Path

import pytest
from fastapi import HTTPException

from website.dashboard import routes as R

APP = (Path(R.__file__).parent / "templates" / "app.html").read_text()


def test_gating_reads_the_long_tail_key_map_lexically():
    assert "window._otherProviders" not in APP
    assert "const others = _otherProviders || {};" in APP


def test_default_selection_falls_back_to_a_usable_others_option():
    body = APP[APP.index("function selectDefaultModel"):]
    body = body[:body.index("\n}\n")]
    assert "radios.forEach(r => { r.checked = false; });" in body
    assert "o.value && !o.disabled" in body


def test_no_key_note_exists_and_is_driven_by_gating():
    assert 'id="model-nokey-note" hidden' in APP
    gating = APP[APP.index("function applyModelGating"):]
    gating = gating[:gating.index("\n}\n")]
    assert "model-nokey-note" in gating


def _msg(keys, model):
    with pytest.raises(HTTPException) as e:
        R._require_model_key(keys, model)
    assert e.value.status_code == 400
    return e.value.detail


def test_key_guard_points_a_deepseek_only_user_at_others():
    msg = _msg({"deepseek": "sk-x", "github_pat": "ghp_x"}, "anthropic/claude-sonnet-5-5")
    assert "DeepSeek in the Others list" in msg
    assert "Github" not in msg and "github" not in msg


def test_key_guard_points_a_vendor_key_at_its_row():
    msg = _msg({"gemini": "g-x"}, "anthropic/claude-sonnet-5-5")
    assert "Gemini in the Gemini row" in msg


def test_key_guard_with_no_provider_key_says_add_one():
    msg = _msg({"github_pat": "ghp_x"}, "deepseek/deepseek-reasoner")
    assert "no DeepSeek API key is configured" in msg


def test_key_guard_with_no_keys_at_all_never_claims_a_key():
    # The ~14 sign-ups with no key: the create endpoint's own "add at least one
    # key" gate answers first, but the guard alone must not invent a key list.
    msg = _msg({}, "anthropic/claude-haiku-5-5")
    assert "no Anthropic API key is configured" in msg
    assert "You do have a key" not in msg


def test_keys_that_cover_the_model_pass():
    R._require_model_key({"deepseek": "sk-x"}, "deepseek/deepseek-reasoner")
    R._require_model_key({"openrouter": "sk-or"}, "anthropic/claude-haiku-5-5")


def test_cheapest_preset_is_haiku_5_5_and_routes_for_openrouter_users():
    assert R._cheapest_model_for({"anthropic": "x"}) == "claude-haiku-5-5"
    m = R._cheapest_model_for({"openrouter": "x"})
    assert R._maybe_route_via_openrouter(R._to_litellm_model(m), {"openrouter": "x"}) \
        == "openrouter/anthropic/claude-haiku-5.5"


@pytest.mark.parametrize("keys, model", [
    ({"gemini": "g-x"}, "gemini-3.8-flash"),
    ({"gemini": "g-x"}, "gemini-3.1-pro-preview"),
    ({"openai": "sk-x"}, "gpt-5.5"),
    ({"openai": "sk-x"}, "gpt-5.4-mini"),
    ({"anthropic": "sk-ant"}, "claude-sonnet-5-5"),
])
def test_vendor_rows_send_bare_names_and_their_own_key_covers_them(keys, model):
    """The Gemini and OpenAI rows post bare names and the create endpoint stores
    them bare. Reading the provider off the raw string called every one of them
    Anthropic, so a Gemini-only or OpenAI-only account was refused its own chips."""
    assert f'value="{model}"' in APP
    R._require_model_key(keys, model)


def test_a_bare_name_still_needs_its_own_vendor_key():
    msg = _msg({"gemini": "g-x"}, "gpt-5.5")
    assert "OpenAI" in msg
