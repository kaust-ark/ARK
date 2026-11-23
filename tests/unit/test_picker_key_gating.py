"""Key validation for new launches and older projects with bare model names."""
import pytest
from fastapi import HTTPException

from website.dashboard import routes as R

def _msg(keys, model):
    with pytest.raises(HTTPException) as e:
        R._require_model_key(keys, model)
    assert e.value.status_code == 400
    return e.value.detail


def test_key_guard_points_a_deepseek_only_user_at_their_group():
    msg = _msg({"deepseek": "sk-x", "github_pat": "ghp_x"}, "anthropic/claude-sonnet-5-5")
    assert "DeepSeek model group" in msg
    assert "Github" not in msg and "github" not in msg


def test_key_guard_points_a_vendor_key_at_its_group():
    msg = _msg({"gemini": "g-x"}, "anthropic/claude-sonnet-5-5")
    assert "Google Gemini model group" in msg


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
def test_owner_key_covers_legacy_projects_with_bare_model_names(keys, model):
    R._require_model_key(keys, model)


def test_a_bare_name_still_needs_its_own_vendor_key():
    msg = _msg({"gemini": "g-x"}, "gpt-5.5")
    assert "OpenAI" in msg
