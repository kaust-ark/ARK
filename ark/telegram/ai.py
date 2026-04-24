"""Haiku-powered message refinement for Telegram notifications.

A single fail-soft function that calls the Anthropic API to polish a raw
notification into a clean, scannable Telegram HTML message. On any error
(no key, network, timeout, bad response) it returns the original text
unchanged. It MUST never raise — the caller is in the Telegram sender
thread and a crash there silently kills future notifications.

Stdlib only (urllib) — no new dependency.
"""

import json
import urllib.error
import urllib.request
from typing import Optional


_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

_SYSTEM_PROMPT = (
    "You are a notification refiner for an autonomous research agent (ARK) "
    "talking to its human operator on Telegram. Rewrite the input message so "
    "it can be scanned in 5 seconds.\n\n"
    "Hard rules:\n"
    "- Preserve every number, score, percentage, ID, file path, and option "
    "label EXACTLY as written. Never invent or guess facts.\n"
    "- Output Telegram HTML only. Allowed tags: <b>, <i>, <code>, <pre>, "
    "<a href=\"...\">. Do NOT use Markdown (no **, no #, no backticks).\n"
    "- Keep numbered options (1., 2., 3., ...) and their order intact. If "
    "the input has 'Custom — ...' as the last option, keep it last.\n"
    "- Add structure with short section headers in <b>...</b> and bullet "
    "lists when helpful, but stay concise. Aim for under 1500 characters.\n"
    "- Never address the operator in second person beyond what is already "
    "in the input. Never add commentary, disclaimers, or sign-offs.\n"
    "- Output ONLY the rewritten message — no preamble, no code fences."
)


def polish_message(raw: str,
                   context: Optional[dict] = None,
                   api_key: str = "",
                   model: str = "claude-haiku-4-5",
                   timeout: float = 8.0) -> str:
    """Polish a Telegram message via Anthropic API.

    Returns the polished text on success, or the original `raw` on any
    failure (missing key, HTTP error, timeout, empty response, parse error).
    Never raises.
    """
    if not raw or not api_key:
        return raw

    ctx = context or {}
    # Compact context block for the model. Keep small to save tokens.
    ctx_lines = []
    for k in ("project", "mode", "iteration", "score", "phase", "kind"):
        v = ctx.get(k)
        if v not in (None, ""):
            ctx_lines.append(f"{k}: {v}")
    ctx_block = "\n".join(ctx_lines) if ctx_lines else "(none)"

    user_content = (
        f"Context:\n{ctx_block}\n\n"
        f"Raw message:\n{raw}\n\n"
        f"Rewrite the raw message per the rules. Output Telegram HTML only."
    )

    payload = {
        "model": model,
        "max_tokens": 800,
        "temperature": 0.2,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_content}],
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            _API_URL,
            data=data,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        parsed = json.loads(body)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return raw
    except Exception:
        return raw

    # Extract first text block
    try:
        blocks = parsed.get("content", [])
        for b in blocks:
            if b.get("type") == "text":
                text = b.get("text", "").strip()
                if text:
                    return text
    except Exception:
        return raw

    return raw
