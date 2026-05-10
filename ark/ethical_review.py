"""Pre-launch ethical review for submitted research ideas.

Calls the Anthropic API with a strict prompt that hard-rejects only clearly
malicious, weaponization-oriented, explicit-sexual, or anti-human proposals.
Everything else (including legitimate dual-use security research) is allowed.

Stdlib only (urllib) — matches the pattern in ark/telegram/ai.py.
Fail-open on infrastructure errors so transient API failures do not silently
block legitimate research.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional


_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

_SYSTEM_PROMPT = """You are an ethics reviewer for ARK, an autonomous research framework. You receive a research idea submitted by a user and decide whether ARK should run it.

BLOCK only ideas that fall clearly and primarily into ONE of these four narrow categories:
1. clearly_malicious_offensive — building tools whose primary purpose is to harm people or systems without authorization (working malware/ransomware for deployment, phishing kits intended for fraud, doxxing tooling, attack infrastructure with no defensive framing).
2. weaponization — operational designs for chemical / biological / radiological / nuclear weapons, IEDs, or other mass-casualty devices intended to harm people.
3. explicit_sexual — sexually explicit content (especially anything involving minors).
4. anti_human — proposals that plan, glorify, or facilitate genocide, mass-casualty violence, terrorism, or other gross violations of human dignity.

ALLOW everything else, including:
- Legitimate security research, vulnerability analysis, defensive tooling, CTF, authorized red-team work, malware analysis with research framing.
- Dual-use AI/ML research; alignment, safety, and adversarial-robustness work.
- Sensitive but legitimate medical, legal, social-science, or policy research.
- Provocative, controversial, or unpopular research questions.
- Ordinary systems / ML / theory / applied engineering papers.

Default to ALLOW when uncertain. The bar for blocking is "clearly and primarily" in one of the four categories above.

Return STRICT JSON only, with NO surrounding prose, NO markdown code fences:
{"decision": "allow" | "block", "category": "<one of the 4 keys above, or 'none'>", "reason": "<one short sentence the user can read>"}"""


def review_idea(
    idea_text: str,
    model: str = "claude-sonnet-4-6",
    api_key: str = "",
    timeout: float = 30.0,
) -> dict:
    """Run ethical review on a research idea.

    Returns a dict with keys ``decision`` (``"allow"`` | ``"block"``),
    ``category``, and ``reason``.

    Fail-open behavior: if the API key is missing, the network call fails, or
    the response cannot be parsed, returns ``decision="allow"`` with the
    failure noted in ``reason``. We prefer not to block legitimate research
    just because telemetry was unreachable.
    """
    if not idea_text or not idea_text.strip():
        return {"decision": "allow", "category": "none", "reason": "empty idea"}

    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {
            "decision": "allow",
            "category": "none",
            "reason": "review skipped — no ANTHROPIC_API_KEY available",
        }

    payload = {
        "model": model,
        "max_tokens": 200,
        "temperature": 0.0,
        "system": _SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Idea to review:\n\n{idea_text}\n\n"
                    f"Return JSON now."
                ),
            }
        ],
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
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as e:
        return {"decision": "allow", "category": "none", "reason": f"review error: {e}"}
    except Exception as e:  # noqa: BLE001 — fail-open by design
        return {"decision": "allow", "category": "none", "reason": f"review error: {e}"}

    text = ""
    for block in parsed.get("content", []) or []:
        if block.get("type") == "text":
            text = (block.get("text") or "").strip()
            if text:
                break

    if not text:
        return {"decision": "allow", "category": "none", "reason": "empty model response"}

    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {"decision": "allow", "category": "none", "reason": "could not parse response"}

    try:
        obj = json.loads(text[start : end + 1])
    except Exception as e:  # noqa: BLE001
        return {"decision": "allow", "category": "none", "reason": f"parse error: {e}"}

    decision = str(obj.get("decision", "allow")).strip().lower()
    if decision not in ("allow", "block"):
        decision = "allow"
    return {
        "decision": decision,
        "category": str(obj.get("category", "none")),
        "reason": str(obj.get("reason", ""))[:500],
    }
