"""The default model and the native chips must stay launchable for everyone.

Most users hold only an OpenRouter key. A native chip reaches them through
_OPENROUTER_SLUG; a chip whose ID is missing there dies at launch with "no key
found". This pins every native chip in the three run pickers, the default
included, to a routable slug, and keeps superseded models routable so old
projects restart on the model they ran.
"""
import re
from pathlib import Path

from website.dashboard import routes as R

APP = Path(R.__file__).parent / "templates" / "app.html"
RUN_PICKERS = ("model", "continue-model", "restart-model")


def _native_chips():
    html = APP.read_text()
    out = {}
    for picker in RUN_PICKERS:
        vals = re.findall(rf'name="{picker}" value="([^"/]+)"( checked)?', html)
        out[picker] = vals
    return out


def test_every_native_chip_routes_through_openrouter():
    keys = {"openrouter": "sk-or-test"}
    for picker, vals in _native_chips().items():
        assert vals, f"no native chips found in {picker}"
        for value, _ in vals:
            routed = R._maybe_route_via_openrouter(R._to_litellm_model(value), keys)
            assert routed.startswith("openrouter/"), f"{picker}: {value} -> {routed}"


def test_default_chip_is_the_default_model_in_every_run_picker():
    for picker, vals in _native_chips().items():
        checked = [v for v, c in vals if c]
        assert checked == [R.DEFAULT_MODEL], f"{picker} preselects {checked}"


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
