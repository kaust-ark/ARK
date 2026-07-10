"""user_actionable_failure: owner-notify only for failures the OWNER can fix.

Positive strings are verbatim from production failures (2026-07); negatives
are platform-side crashes that must stay admin-only.
"""

from website.dashboard.notify import user_actionable_failure


def test_credit_exhaustion_variants_match():
    assert user_actionable_failure(
        'APIError: OpenrouterException - {"error":{"message":"Insufficient credits. '
        'This account never purchased credits."}}')
    assert user_actionable_failure(
        'This request requires more credits, or fewer max_tokens. You requested up to 8192')
    assert user_actionable_failure("exceeded your current quota, please check your plan")


def test_key_problems_match():
    assert user_actionable_failure(
        'DeepseekException - {"error":{"message":"Authentication Fails, Your api key: '
        '****6c53 is invalid"}}')
    assert user_actionable_failure(
        "model is 'deepseek/deepseek-chat' but no key found — set deepseek_api_key")
    assert user_actionable_failure(
        "Launch failed: the selected model needs a provider API key that isn't configured")


def test_platform_failures_stay_admin_only():
    assert not user_actionable_failure("AttributeError: 'list' object has no attribute 'get'")
    assert not user_actionable_failure("Termination signal 15 received. cleaning up...")
    assert not user_actionable_failure(
        "RuntimeError: systemd-run failed for ark-orch-x (rc=1)")
    assert not user_actionable_failure("")
    assert not user_actionable_failure(None)
