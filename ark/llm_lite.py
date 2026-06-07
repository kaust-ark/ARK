"""Lightweight LiteLLM helper for ARK's non-agent ("bucket 2") text calls.

These are simple one-shot LLM calls — paper title, iteration summary, Telegram
bot replies, message classification — that do NOT need the OpenHands agent loop
(no files, no bash, no multi-step autonomy). They go straight through LiteLLM so
they are provider-agnostic exactly like the heavy agents, which means a single
non-Anthropic key can run the entire pipeline.

The heavy research agents do NOT use this module — they run through OpenHands
(which has its own internal LiteLLM). This is only for the light helpers.
"""
from __future__ import annotations

import os
from typing import Optional

# Most LiteLLM providers read their key from <PROVIDER>_API_KEY (DEEPSEEK_API_KEY,
# XAI_API_KEY, MISTRAL_API_KEY, …). Only providers whose env var does NOT follow
# that convention are listed here; everything else is derived — so ANY provider
# OpenHands/LiteLLM supports works, not just the mainstream three.
_PROVIDER_KEY_OVERRIDES = {
    "gemini": "GEMINI_API_KEY",
    "vertex_ai": "GEMINI_API_KEY",
}


def provider_key_env(provider: str) -> str:
    """Env var holding the API key for a LiteLLM provider prefix.

    Falls back to the ``<PROVIDER>_API_KEY`` convention, covering the long tail
    (deepseek, xai, mistral, groq, …), not just anthropic/openai/gemini.
    """
    p = (provider or "").lower()
    return _PROVIDER_KEY_OVERRIDES.get(p, f"{p.upper()}_API_KEY")

# Cheap "utility" model per provider, for the light helpers that have no project
# config to read a bot_model from. Picked by whichever API key is present, so a
# single non-Anthropic key still runs every helper.
_UTILITY_MODEL_BY_PROVIDER = {
    "anthropic": "anthropic/claude-haiku-4-5",
    "gemini": "gemini/gemini-2.5-flash",
    "openai": "openai/gpt-4o-mini",
}
DEFAULT_UTILITY_MODEL = _UTILITY_MODEL_BY_PROVIDER["anthropic"]


def load_api_keys_into_env(config: dict) -> None:
    """Export ``config.yaml`` API keys into ``os.environ``.

    Both the heavy-agent runtime (``OpenHandsCLI.build_env``) and the light
    helpers (``llm_lite``) read provider keys from the environment; users put
    their keys in ``config.yaml`` — this bridges the two. Call once at startup
    (orchestrator init + CLI entry). Existing env vars win (e.g. webapp-injected),
    so this only fills gaps.

    Loads ANY ``<provider>_api_key`` field (anthropic_api_key, deepseek_api_key,
    xai_api_key, …) into that provider's conventional env var, so the full set of
    OpenHands/LiteLLM providers is supported — not just the mainstream three.
    """
    if not config:
        return
    for field, val in config.items():
        if not (isinstance(field, str) and field.endswith("_api_key") and val):
            continue
        env_var = provider_key_env(field[: -len("_api_key")])
        if not os.environ.get(env_var):
            os.environ[env_var] = str(val)
    # GOOGLE_API_KEY alias that some Google SDKs read.
    gkey = config.get("gemini_api_key")
    if gkey and not os.environ.get("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = str(gkey)


def default_utility_model() -> str:
    """Return a cheap model for whichever provider has a key in the environment.

    Lets the config-less light helpers (title, status summary, classify) run on
    whatever single key the user provided, instead of hard-failing on Anthropic.
    """
    for provider, env in (("anthropic", "ANTHROPIC_API_KEY"),
                          ("gemini", "GEMINI_API_KEY"),
                          ("openai", "OPENAI_API_KEY")):
        if os.environ.get(env):
            return _UTILITY_MODEL_BY_PROVIDER[provider]
    return DEFAULT_UTILITY_MODEL


def utility_model() -> str:
    """Model for the config-less light helpers (title / idea analysis / classify).

    Prefers the global ``.ark/config.yaml`` ``bot_model`` (then ``model``) so the
    user CAN specify what these use — falling back to a cheap model for whichever
    API key is present. This is how a config-less helper still honours user intent.
    """
    try:
        import yaml
        from ark.paths import get_config_dir
        gcfg = get_config_dir() / "config.yaml"
        if gcfg.exists():
            c = yaml.safe_load(gcfg.read_text()) or {}
            m = c.get("bot_model") or c.get("model")
            if m and "/" in str(m):
                return str(m)
    except Exception:
        pass
    return default_utility_model()


def resolve_key(model: str, explicit: Optional[str] = None) -> Optional[str]:
    """Return the API key for ``model``'s provider (prefix-based), or None."""
    if explicit:
        return explicit
    provider = model.split("/", 1)[0] if "/" in model else ""
    if not provider:
        return None
    return os.environ.get(provider_key_env(provider)) or None


def complete(
    prompt: str,
    *,
    model: str,
    system: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: int = 60,
    max_tokens: Optional[int] = None,
) -> str:
    """One-shot LiteLLM completion. Returns the response text ("" on failure).

    ``model`` is a LiteLLM model string, e.g. ``anthropic/claude-haiku-4-5`` /
    ``gemini/gemini-2.5-flash`` / ``openai/gpt-4o-mini``. The provider prefix
    selects which API key env var to use unless ``api_key`` is given.

    Fail-soft by design (returns ""): these helpers are non-critical UX touches;
    callers already handle an empty result gracefully.
    """
    import litellm

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        resp = litellm.completion(
            model=model,
            messages=messages,
            api_key=resolve_key(model, api_key),
            timeout=timeout,
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""
