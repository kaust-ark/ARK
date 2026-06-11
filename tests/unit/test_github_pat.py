"""Tests for the GitHub PAT auto-push feature.

Security-focused: the orchestrator-only ARK_GITHUB_PAT (and GITHUB_TOKEN) must
never be inherited by the autonomous agent subprocess. These tests do NOT touch
the network or the real GitHub API.
"""
import os

from ark.engines.cli import AgentCLI, OpenHandsCLI


class _DummyAgentCLI(AgentCLI):
    """Concrete subclass so we can instantiate the ABC for build_env tests."""

    def build_command(self, prompt, path_boundary, code_dir):  # pragma: no cover
        return ["true"]


def test_agent_env_strips_github_pat(monkeypatch):
    monkeypatch.setenv("ARK_GITHUB_PAT", "ghp_secret_value")
    monkeypatch.setenv("GITHUB_TOKEN", "gho_other_secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-keep-me")

    env = _DummyAgentCLI("claude").build_env()

    assert "ARK_GITHUB_PAT" not in env
    assert "GITHUB_TOKEN" not in env
    # Non-sensitive keys still pass through.
    assert env.get("ANTHROPIC_API_KEY") == "sk-keep-me"
    # The secret value must not leak into the agent env under any key.
    assert "ghp_secret_value" not in env.values()
    assert "gho_other_secret" not in env.values()


def test_openhands_env_strips_github_pat(monkeypatch):
    monkeypatch.setenv("ARK_GITHUB_PAT", "ghp_secret_value")

    env = OpenHandsCLI("claude").build_env()

    assert "ARK_GITHUB_PAT" not in env
    assert "ghp_secret_value" not in env.values()
