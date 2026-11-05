"""The headless CLI gets an output-token cap through a persisted agent spec.

Without one, OpenHands takes max_output_tokens from litellm's downloaded model
table: 393,216 for deepseek-v4-pro. OpenRouter reserves credit for the whole
cap, so d5db85a2 was refused about 120 times while it could still pay for
ordinary replies. Measured with the production CLI (1.16.0) against a local
fake endpoint: requests carried max_tokens 393216 without the spec and 32768
with it.
"""
import json
import sys
from pathlib import Path

import pytest

import ark.engines.cli as cli
from ark.engines.cli import OpenHandsCLI


def _fake_interp(tmp_path, cap_echo=True):
    py = tmp_path / "ohpy"
    body = ("import json,sys\nargs=[a for a in sys.argv[1:] if a not in ('-I','-c')]\n"
            "model,cap=args[-2],int(args[-1])\n"
            "open(%r,'a').write('called\\n')\n"
            "print(json.dumps({'llm':{'model':model,'max_output_tokens':cap if %r else 1}}))\n"
            % (str(tmp_path / "calls"), cap_echo))
    py.write_text(f"#!{sys.executable}\n{body}")
    py.chmod(0o755)
    return str(py)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ARK_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.setenv("ARK_OPENHANDS_PYTHON", _fake_interp(tmp_path))
    (tmp_path / "proj").mkdir()
    return tmp_path


def _calls(root):
    f = root / "calls"
    return len(f.read_text().splitlines()) if f.exists() else 0


def test_the_cli_runs_with_a_capped_spec_in_the_project(project):
    env = OpenHandsCLI("openrouter/deepseek/deepseek-v4-pro").build_env(project / "proj")
    persist = project / "proj" / ".openhands"
    assert env["OPENHANDS_PERSISTENCE_DIR"] == str(persist.resolve())
    spec = json.loads((persist / "agent_settings.json").read_text())
    assert spec["llm"]["max_output_tokens"] == 32768
    # conversations stay where ARK reads their usage
    assert env["OPENHANDS_CONVERSATIONS_DIR"] == str(project / ".openhands" / "conversations")


def test_the_spec_is_generated_once_per_model_and_cap(project):
    OpenHandsCLI("deepseek/deepseek-reasoner").build_env(project / "proj")
    OpenHandsCLI("deepseek/deepseek-reasoner").build_env(project / "proj")
    assert _calls(project) == 1
    OpenHandsCLI("deepseek/deepseek-chat").build_env(project / "proj")
    assert _calls(project) == 2


def test_the_cap_is_configurable_and_can_be_switched_off(project, monkeypatch):
    monkeypatch.setenv("ARK_MAX_OUTPUT_TOKENS", "8192")
    OpenHandsCLI("deepseek/deepseek-reasoner").build_env(project / "proj")
    spec = json.loads((project / "proj" / ".openhands" / "agent_settings.json").read_text())
    assert spec["llm"]["max_output_tokens"] == 8192
    monkeypatch.setenv("ARK_MAX_OUTPUT_TOKENS", "0")
    env = OpenHandsCLI("deepseek/deepseek-reasoner").build_env(project / "proj")
    assert "OPENHANDS_PERSISTENCE_DIR" not in env


def test_a_spec_without_the_cap_is_never_written(project, monkeypatch):
    monkeypatch.setenv("ARK_OPENHANDS_PYTHON", _fake_interp(project, cap_echo=False))
    OpenHandsCLI("deepseek/deepseek-reasoner").build_env(project / "proj")
    assert not (project / "proj" / ".openhands" / "agent_settings.json").exists()


def test_without_an_openhands_interpreter_the_cli_runs_as_before(project, monkeypatch):
    monkeypatch.setenv("ARK_OPENHANDS_PYTHON", str(project / "missing"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    env = OpenHandsCLI("deepseek/deepseek-reasoner").build_env(project / "proj")
    assert env["LLM_MODEL"] == "deepseek/deepseek-reasoner"
    assert not (project / "proj" / ".openhands" / "agent_settings.json").exists()


PROD_OH = Path("/data/fat/ark/tools/uv-tools/openhands/bin/python")


@pytest.mark.skipif(not PROD_OH.exists(), reason="production OpenHands tool env not present")
def test_the_real_openhands_builds_a_loadable_capped_spec(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ARK_OPENHANDS_PYTHON", str(PROD_OH))
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")   # no network in tests
    (tmp_path / "proj").mkdir()
    OpenHandsCLI("openai/fixture").build_env(tmp_path / "proj")
    settings = tmp_path / "proj" / ".openhands" / "agent_settings.json"
    out = cli.subprocess.run([str(PROD_OH), "-I", "-c",
                              "import sys; from openhands.sdk import Agent; "
                              "a = Agent.model_validate_json(open(sys.argv[1]).read()); "
                              "print(a.llm.max_output_tokens, a.condenser.llm.max_output_tokens)",
                              str(settings)], capture_output=True, text=True, timeout=180,
                             env={"HOME": str(tmp_path), "OPENHANDS_SUPPRESS_BANNER": "1",
                                  "LITELLM_LOCAL_MODEL_COST_MAP": "True", "PATH": "/usr/bin:/bin"})
    assert out.stdout.split() == ["32768", "32768"], out.stderr[-500:]
