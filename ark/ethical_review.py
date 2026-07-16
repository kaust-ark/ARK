"""Pre-launch gate (Gate A) for submitted research ideas.

This is the FLIGHT-PREFLIGHT gate: it runs before any compute is spent and has
no literature context yet, so it judges only two things — (1) ethical
admissibility and (2) basic scientific soundness (is the idea absurd,
pseudoscientific, or physically impossible?). Novelty / prior-art / scope /
contribution are judged later by Gate B, which runs after Deep Research and has
the literature in hand.

Calls the run's SELECTED model (provider-agnostic via LiteLLM) with a strict
prompt that hard-rejects only clearly malicious, weaponization-oriented,
explicit-sexual, anti-human, or absurd/pseudoscientific proposals. Everything
else (including legitimate dual-use security research) is allowed.

Stdlib only (urllib) — matches the pattern in ark/telegram/ai.py.
Fail-open on infrastructure errors so transient API failures do not silently
block legitimate research.
"""

from __future__ import annotations

import json

_SYSTEM_PROMPT = """You are an ethics-and-soundness reviewer for ARK, an autonomous research framework. You receive a research idea submitted by a user and decide whether ARK should run it. You judge two things only at this stage (no literature is available yet): (1) ethical admissibility, (2) basic scientific soundness — is the idea absurd, pseudoscientific, or physically impossible?

REJECT (hard block) only ideas that fall clearly and primarily into ONE of these categories:
1. clearly_malicious_offensive — building tools whose primary purpose is to harm people or systems without authorization (working malware/ransomware for deployment, phishing kits intended for fraud, doxxing tooling, attack infrastructure with no defensive framing).
2. weaponization — operational designs for chemical / biological / radiological / nuclear weapons, IEDs, or other mass-casualty devices intended to harm people.
3. explicit_sexual — sexually explicit content (especially anything involving minors).
4. anti_human — proposals that plan, glorify, or facilitate genocide, mass-casualty violence, terrorism, or other gross violations of human dignity.
5. absurd_pseudoscience — the idea contradicts well-established physical law (e.g. perpetual motion, faster-than-light signalling, over-unity / "free energy" devices), relies on debunked pseudoscience (astrology-as-mechanism, homeopathy-as-validated), or is internally incoherent to the point that no experiment could test it. The bar is HIGH: only reject when a competent scientist in the field would call it impossible-on-its-face, NOT merely unconventional, speculative, contrarian, or unlikely-to-work.

ALLOW everything else, including:
- Legitimate security research, vulnerability analysis, defensive tooling, CTF, authorized red-team work, malware analysis with research framing.
- Dual-use AI/ML research; alignment, safety, and adversarial-robustness work.
- Sensitive but legitimate medical, legal, social-science, or policy research.
- Provocative, controversial, unpopular, high-risk, or unconventional research questions, including long-shot or speculative ideas that are nonetheless physically possible and testable.
- Ordinary systems / ML / theory / applied engineering papers.

Default to ALLOW when uncertain. The bar for REJECT is "clearly and primarily" in one of the five categories above. Novelty, prior-art, scope, and contribution are NOT judged here (no literature is available yet) — never reject for "already done", "too ambitious", or "too broad".

Return STRICT JSON only, with NO surrounding prose, NO markdown code fences:
{"verdict": "proceed" | "refine" | "human_review" | "reject", "category": "<one of the 5 keys above, or 'none'>", "reason": "<one short sentence the user can read>", "scores": {"ethics": 1-5, "feasibility": 1-5, "scientific_value": 1-5}}

Verdict guidance: "reject" = clearly in a category above. "human_review" = a borderline ethics case you genuinely can't call (rare). "refine" = admissible but you'd flag a soundness concern worth telling the user (non-blocking). "proceed" = fine. Scores: ethics 5 = no concern, 1 = hard violation; feasibility 5 = physically plausible, 1 = impossible; scientific_value is a rough first impression. When in doubt, prefer "proceed" or "refine" over "human_review", and never use "reject" outside the five categories."""


# The gate's judge is FIXED per key family — never the run's own model. Using
# the defendant's chosen model made the verdict vary by user (a reasoning
# model at provider-default temperature flipped reject→proceed on an identical
# idea 26 minutes apart, 2026-07-16). Cheap, stable chat models only.
_JUDGE_BY_FAMILY = {
    "openrouter": "openrouter/openai/gpt-4o-mini",
    "anthropic": "anthropic/claude-haiku-4-5",
    "openai": "openai/gpt-4o-mini",
    "gemini": "gemini/gemini-2.5-flash",
}


def _judge_for(run_model: str) -> str:
    """Fixed judge slug reachable with the run's key; falls back to the run
    model for long-tail direct keys (deepseek/... etc.) where no other slug
    is reachable."""
    family = (run_model or "").split("/", 1)[0].lower()
    return _JUDGE_BY_FAMILY.get(family, run_model)


def review_idea(
    idea_text: str,
    model: str = "",
    api_key: str = "",
    timeout: float = 30.0,
) -> dict:
    """Run Gate A review on a research idea, using the run's SELECTED model.

    Returns a dict with keys:
      - ``verdict`` — ``"proceed" | "refine" | "human_review" | "reject"``
      - ``decision`` — legacy ``"allow" | "block"`` derived from ``verdict``
        (``reject`` → ``block``, everything else → ``allow``) so existing
        callers and cached verdicts keep working unchanged.
      - ``category``, ``reason``
      - ``scores`` — ``{"ethics", "feasibility", "scientific_value"}`` (values
        may be ``None`` when not returned / on a fail-open skip)
      - ``reviewed`` — True only when the model actually returned a parseable
        verdict, so the caller can tell a real pass from a fail-open skip.

    Fail-open: if no model is configured, the call fails, or the response can't
    be parsed, returns ``verdict="proceed"`` / ``decision="allow"`` with
    ``reviewed=False`` and the cause in ``reason``. We don't block legitimate
    research on an infra hiccup, but such a skip is no longer reported as a
    genuine "pass".

    The model is provider-agnostic (resolved via LiteLLM inside ``complete``),
    so the review runs on whatever model — and already-verified key — the run
    uses, instead of a hardcoded Anthropic endpoint.
    """
    if not idea_text or not idea_text.strip():
        return _fail_open("empty idea")
    if not model:
        return _fail_open("review skipped — no model configured")

    judge = _judge_for(model)
    from ark.llm_lite import complete
    try:
        # temperature=0: a gate must not sample its verdict.
        text = complete(
            f"Idea to review:\n\n{idea_text}\n\nReturn JSON now.",
            model=judge,
            system=_SYSTEM_PROMPT,
            api_key=api_key or None,
            timeout=int(timeout),
            temperature=0.0,
        )
    except Exception as e:  # noqa: BLE001 — fail-open by design
        return _fail_open(f"review error: {e}")

    text = (text or "").strip()
    if not text:
        return _fail_open("review error: empty model response")

    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return _fail_open("could not parse response")

    try:
        obj = json.loads(text[start : end + 1])
    except Exception as e:  # noqa: BLE001
        return _fail_open(f"parse error: {e}")

    verdict = str(obj.get("verdict", obj.get("decision", "proceed"))).strip().lower()
    # Tolerate a model that still emits the legacy decision vocabulary.
    if verdict == "block":
        verdict = "reject"
    elif verdict == "allow":
        verdict = "proceed"
    if verdict not in ("proceed", "refine", "human_review", "reject"):
        verdict = "proceed"
    # A hard block gets a second opinion: two confirmation samples (temp 0.7
    # so they are genuinely independent draws). If NEITHER confirms, the reject
    # was an outlier — downgrade to human_review, whose HITL flow still
    # defaults to block if nobody answers. Costs two cheap calls only on the
    # (rare) reject path; protects users from a spuriously sampled rejection.
    if verdict == "reject":
        confirms = 0
        for _ in range(2):
            v2 = _sample_verdict(idea_text, judge, api_key, timeout, temperature=0.7)
            if v2 == "reject":
                confirms += 1
        if confirms == 0:
            verdict = "human_review"
    # Legacy derivation — reject => block, everything else => allow. Keeps the
    # existing `if not _run_ethical_review()` caller and old caches working.
    decision = "block" if verdict == "reject" else "allow"
    raw_scores = obj.get("scores") or {}
    scores = {k: raw_scores.get(k) for k in ("ethics", "feasibility", "scientific_value")}
    return {
        "verdict": verdict,
        "decision": decision,
        "category": str(obj.get("category", "none")),
        "reason": str(obj.get("reason", ""))[:500],
        "scores": scores,
        "reviewed": True,
    }


def _sample_verdict(idea_text: str, judge: str, api_key: str,
                    timeout: float, temperature: float) -> str:
    """One extra verdict draw for reject confirmation; '' on any failure."""
    from ark.llm_lite import complete
    try:
        t = complete(f"Idea to review:\n\n{idea_text}\n\nReturn JSON now.",
                     model=judge, system=_SYSTEM_PROMPT,
                     api_key=api_key or None, timeout=int(timeout),
                     temperature=temperature) or ""
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            return ""
        o = json.loads(t[start:end + 1]) or {}
        v = str(o.get("verdict", o.get("decision", ""))).strip().lower()
        return "reject" if v in ("reject", "block") else v
    except Exception:
        return ""


def _fail_open(reason: str) -> dict:
    """Uniform fail-open verdict: never block on an infra/parse hiccup."""
    return {
        "verdict": "proceed",
        "decision": "allow",
        "category": "none",
        "reason": reason,
        "scores": {"ethics": None, "feasibility": None, "scientific_value": None},
        "reviewed": False,
    }
