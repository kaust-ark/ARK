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
    model: str = "",
    api_key: str = "",
    timeout: float = 30.0,
) -> dict:
    """Run ethical review on a research idea, using the run's SELECTED model.

    Returns a dict with keys ``decision`` (``"allow"`` | ``"block"``),
    ``category``, ``reason``, and ``reviewed`` — ``reviewed`` is True only when
    the model actually returned a parseable verdict, so the caller can tell a
    real pass from a fail-open skip.

    Fail-open: if no model is configured, the call fails, or the response can't
    be parsed, returns ``decision="allow"`` with ``reviewed=False`` and the
    cause in ``reason``. We don't block legitimate research on an infra hiccup,
    but such a skip is no longer reported as a genuine "pass".

    The model is provider-agnostic (resolved via LiteLLM inside ``complete``),
    so the review runs on whatever model — and already-verified key — the run
    uses, instead of a hardcoded Anthropic endpoint.
    """
    if not idea_text or not idea_text.strip():
        return {"decision": "allow", "category": "none", "reason": "empty idea", "reviewed": False}
    if not model:
        return {"decision": "allow", "category": "none", "reason": "review skipped — no model configured", "reviewed": False}

    from ark.llm_lite import complete
    try:
        text = complete(
            f"Idea to review:\n\n{idea_text}\n\nReturn JSON now.",
            model=model,
            system=_SYSTEM_PROMPT,
            api_key=api_key or None,
            timeout=int(timeout),
        )
    except Exception as e:  # noqa: BLE001 — fail-open by design
        return {"decision": "allow", "category": "none", "reason": f"review error: {e}", "reviewed": False}

    text = (text or "").strip()
    if not text:
        return {"decision": "allow", "category": "none", "reason": "review error: empty model response", "reviewed": False}

    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {"decision": "allow", "category": "none", "reason": "could not parse response", "reviewed": False}

    try:
        obj = json.loads(text[start : end + 1])
    except Exception as e:  # noqa: BLE001
        return {"decision": "allow", "category": "none", "reason": f"parse error: {e}", "reviewed": False}

    decision = str(obj.get("decision", "allow")).strip().lower()
    if decision not in ("allow", "block"):
        decision = "allow"
    return {
        "decision": decision,
        "category": str(obj.get("category", "none")),
        "reason": str(obj.get("reason", ""))[:500],
        "reviewed": True,
    }
