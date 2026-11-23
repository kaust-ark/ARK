"""Legacy project model names keep their existing OpenRouter billing routes."""

from website.dashboard import routes as R


def test_legacy_native_slugs_still_route_through_openrouter():
    for native, slug in R._OPENROUTER_SLUG.items():
        assert R._maybe_route_via_openrouter(native, {"openrouter": "configured"}) == f"openrouter/{slug}"


def test_default_model_routes_to_its_openrouter_slug():
    keys = {"openrouter": "sk-or-test"}
    assert R._maybe_route_via_openrouter(R._to_litellm_model(R.DEFAULT_MODEL), keys) \
        == "openrouter/anthropic/claude-sonnet-5.5"
    assert R._to_litellm_model("") == f"anthropic/{R.DEFAULT_MODEL}"


def test_superseded_models_still_route_for_old_projects():
    keys = {"openrouter": "sk-or-test"}
    for old in ("claude-sonnet-4-6", "claude-opus-4-8", "gemini-3.5-flash", "gemini-2.5-pro"):
        assert R._maybe_route_via_openrouter(R._to_litellm_model(old), keys).startswith("openrouter/")


def test_direct_key_still_wins_over_openrouter():
    keys = {"openrouter": "sk-or-test", "anthropic": "sk-ant-test"}
    assert R._maybe_route_via_openrouter("anthropic/claude-sonnet-5-5", keys) == "anthropic/claude-sonnet-5-5"
