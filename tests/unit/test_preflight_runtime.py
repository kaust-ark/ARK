"""Preflight checks the runtime the run will use, not always OpenHands.

A host with only the DSH SDK exited at startup on "'openhands' command not
found", and a gateway reached with LLM_API_KEY was refused for having no
provider key, although both runtimes accept that key.
"""
import sys

import pytest

from ark.pipeline import PipelineMixin


def _pipe(model="openai/ext/model"):
    p = PipelineMixin.__new__(PipelineMixin)
    p.model, p.config, p.logs = model, {}, []
    p.log = lambda m, l="INFO": p.logs.append((l, m))
    return p


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("ARK_AGENT_RUNTIME", "ARK_DSH_PYTHON", "OPENAI_API_KEY", "LLM_API_KEY", "LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    # Only the OpenHands CLI is missing; the LaTeX tools checked later exist.
    monkeypatch.setattr("shutil.which", lambda name: None if name == "openhands" else f"/usr/bin/{name}")
    return monkeypatch


def _fake_sdk(tmp_path, versions):
    py = tmp_path / "py"
    py.write_text(f"#!{sys.executable}\nprint({versions!r})\n")
    py.chmod(0o755)
    return str(py)


def test_openhands_runtime_still_requires_the_cli(clean_env):
    clean_env.setenv("OPENAI_API_KEY", "k")
    with pytest.raises(SystemExit):
        _pipe().check_dependencies()


def test_a_dsh_host_needs_no_openhands(clean_env, tmp_path):
    clean_env.setenv("ARK_AGENT_RUNTIME", "dsh")
    clean_env.setenv("ARK_DSH_PYTHON", _fake_sdk(tmp_path, "0.2.1rc2 0.2.1rc2"))
    clean_env.setenv("OPENAI_API_KEY", "k")
    _pipe().check_dependencies()  # no exit


def test_a_wrong_dsh_version_is_refused(clean_env, tmp_path):
    clean_env.setenv("ARK_AGENT_RUNTIME", "dsh")
    clean_env.setenv("ARK_DSH_PYTHON", _fake_sdk(tmp_path, "0.2.2rc1 0.2.2rc1"))
    clean_env.setenv("OPENAI_API_KEY", "k")
    p = _pipe()
    with pytest.raises(SystemExit):
        p.check_dependencies()
    assert any("0.2.1rc2" in m for _, m in p.logs)


def test_a_gateway_key_counts_as_the_provider_key(clean_env, tmp_path):
    clean_env.setenv("ARK_AGENT_RUNTIME", "dsh")
    clean_env.setenv("ARK_DSH_PYTHON", _fake_sdk(tmp_path, "0.2.1rc2 0.2.1rc2"))
    clean_env.setenv("LLM_BASE_URL", "http://gw.invalid/v1")
    clean_env.setenv("LLM_API_KEY", "k")
    _pipe().check_dependencies()  # no exit


def test_a_per_role_model_needs_its_own_key(clean_env, tmp_path):
    clean_env.setenv("ARK_AGENT_RUNTIME", "dsh")
    clean_env.setenv("ARK_DSH_PYTHON", _fake_sdk(tmp_path, "0.2.1rc2 0.2.1rc2"))
    clean_env.setenv("OPENAI_API_KEY", "k")
    p = _pipe()
    p.config = {"agent_models": {"reviewer": "anthropic/claude-sonnet-5-5"}}
    with pytest.raises(SystemExit):
        p.check_dependencies()
    assert any("agent_models.reviewer" in m for _, m in p.logs)
    clean_env.setenv("ANTHROPIC_API_KEY", "k2")
    p.logs.clear()
    p.check_dependencies()  # no exit


def test_roles_run_on_their_configured_model():
    from ark.engines import AgentMixin
    o = AgentMixin()
    o.model, o.config = "deepseek/deepseek-v4-pro", {"agent_models": {"reviewer": "anthropic/claude-sonnet-5-5"}}
    assert o._model_for("reviewer") == "anthropic/claude-sonnet-5-5"
    assert o._model_for("writer") == "deepseek/deepseek-v4-pro"
    o.config = {}
    assert o._model_for("reviewer") == "deepseek/deepseek-v4-pro"
