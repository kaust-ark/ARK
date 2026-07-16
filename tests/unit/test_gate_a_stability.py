"""Gate A stability: fixed judge, zero-temperature verdicts, reject confirmation.

Born from 2026-07-16: the same unmodified idea was rejected at 05:39 and
passed-with-concern at 06:05 — the gate sampled its verdict on the run's own
model at provider-default temperature.
"""

import json
from unittest.mock import patch

from ark.ethical_review import review_idea, _judge_for


def _verdict_json(verdict, category="none", reason="r"):
    return json.dumps({"verdict": verdict, "category": category, "reason": reason,
                       "scores": {"ethics": 3, "feasibility": 3, "scientific_value": 3}})


def test_judge_is_fixed_not_the_runs_model():
    assert _judge_for("openrouter/deepseek/deepseek-reasoner") == "openrouter/openai/gpt-4o-mini"
    assert _judge_for("anthropic/claude-opus-4-8") == "anthropic/claude-haiku-4-5"
    # long-tail direct key: no other slug reachable -> keep the run model
    assert _judge_for("deepseek/deepseek-chat") == "deepseek/deepseek-chat"


def test_primary_verdict_uses_temperature_zero():
    calls = []
    def fake_complete(prompt, **kw):
        calls.append(kw)
        return _verdict_json("proceed")
    with patch("ark.llm_lite.complete", side_effect=fake_complete):
        r = review_idea("a benign idea", model="openrouter/deepseek/deepseek-reasoner")
    assert r["verdict"] == "proceed" and r["reviewed"]
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["model"] == "openrouter/openai/gpt-4o-mini"


def test_confirmed_reject_stands():
    seq = [_verdict_json("reject", "absurd_pseudoscience"),
           _verdict_json("reject"), _verdict_json("proceed")]
    with patch("ark.llm_lite.complete", side_effect=seq):
        r = review_idea("bad idea", model="openai/gpt-4o")
    assert r["verdict"] == "reject" and r["decision"] == "block"


def test_unconfirmed_reject_downgrades_to_human_review():
    seq = [_verdict_json("reject", "absurd_pseudoscience"),
           _verdict_json("proceed"), _verdict_json("refine")]
    with patch("ark.llm_lite.complete", side_effect=seq):
        r = review_idea("borderline idea", model="openai/gpt-4o")
    assert r["verdict"] == "human_review"
    assert r["decision"] == "allow"  # HITL path decides; its timeout default blocks


def test_fail_open_unchanged():
    with patch("ark.llm_lite.complete", side_effect=OSError("api down")):
        r = review_idea("idea", model="openai/gpt-4o")
    assert r["verdict"] == "proceed" and r["reviewed"] is False
