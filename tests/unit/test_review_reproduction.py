import subprocess
import sys

import pytest

from ark.compute import review_control


LOCAL = {"backend": "local", "image": "sha256:" + "1" * 64}


@pytest.fixture
def trusted_driver(monkeypatch):
    """Exercise the same driver/pipe limits with trusted inputs in offline unit tests."""
    popen = subprocess.Popen
    invocations = []

    def launch(command, **options):
        request = options["stdin"]
        wire = request.read()
        request.seek(0)
        invocations.append((command, wire))
        return popen([sys.executable, "-I", "-B", "-c", command[-1]], **options)

    monkeypatch.setattr(review_control.subprocess, "Popen", launch)
    return invocations


def test_reproduction_sees_only_its_program_and_identifiers(tmp_path, monkeypatch, trusted_driver):
    import json

    monkeypatch.chdir(tmp_path)
    (tmp_path / "submission.py").write_text("raise RuntimeError('worker source loaded')")
    (tmp_path / "measurements.json").write_text('{"answer":9999}')
    program = (
        "from pathlib import Path\n"
        "def reproduce(identifiers):\n"
        "    assert list(Path.cwd().iterdir()) == []\n"
        "    assert identifiers == {'seed': 3, 'condition': 'raw'}\n"
        "    return {'score': identifiers['seed'] / 2, 'n': 4}\n"
        "if __name__ == '__main__':\n"
        "    raise RuntimeError('full experiment launched')"
    )
    ids = {"seed": 3, "condition": "raw"}
    result = review_control.reproduce(LOCAL, program, ids, ["n", "score"])
    assert list(result.items()) == [("n", 4), ("score", 1.5)]
    (command, wire), = trusted_driver
    assert json.loads(wire) == {"program": program, "identifiers": ids}
    assert command[:4] == ["docker", "run", "--rm", "-i"]
    assert command[command.index("--network") + 1] == "none"
    assert "--mount" not in command and "-v" not in command and str(tmp_path) not in command
    assert "--read-only" in command and command[command.index("--entrypoint") + 1] == "timeout"


@pytest.mark.parametrize("body", [
    "{'different': 1}", "{'score': True}", "{'score': [1]}", "{'score': '1'}", "{'score': float('inf')}",
    "{'score': 1, 'extra': 1}", "{}", "1",
])
def test_invalid_or_missing_reproduction_column_fails(body, trusted_driver):
    with pytest.raises(ValueError):
        review_control.reproduce(LOCAL, f"def reproduce(identifiers): return {body}", {}, ["score"])


@pytest.mark.parametrize("program,ids,columns", [
    ("", {}, ["x"]), ("a" * 16001, {}, ["x"]), ("p", {"k": ["row"]}, ["x"]),
    ("p", {"k": float("nan")}, ["x"]), ("p", {"k": 10 ** 400}, ["x"]),
    ("p", {"x": 1}, ["x"]), ("p", {}, ["x", "x"]), ("p", {}, []),
])
def test_invalid_request_never_starts_science_runtime(program, ids, columns, monkeypatch):
    monkeypatch.setattr(review_control, "_execute", lambda *a, **kw: pytest.fail("runtime launched"))
    with pytest.raises(ValueError):
        review_control.reproduce(LOCAL, program, ids, columns)


def test_reproduction_output_cap_applies_to_both_streams(trusted_driver):
    for fd in (1, 2):
        source = (f"import os\ndef reproduce(ids):\n    os.write({fd}, b'x'*9000)\n"
                  "    return {'score': 0}")
        with pytest.raises(ValueError, match="output limit"):
            review_control.reproduce(LOCAL, source, {}, ["score"])


def test_slurm_receipt_uses_local_image_without_submitting(trusted_driver, monkeypatch):
    invocation = ["apptainer", "exec", "--containall", "--no-home", "--network", "none"]
    monkeypatch.setattr(review_control, "apptainer_command", lambda: (invocation.copy(), "/science.sif"))
    assert review_control.reproduce({"backend": "slurm"}, "def reproduce(ids): return {'score': 4}",
                                    {}, ["score"]) == {"score": 4}
    (command, _), = trusted_driver
    assert command[:len(invocation)] == invocation
    assert "/science.sif" in command and "timeout" in command and "sbatch" not in command


def test_overdue_reproduction_is_stopped(trusted_driver, monkeypatch):
    monkeypatch.setattr(review_control, "_WALL_SECONDS", 0.25)
    source = "import time\ndef reproduce(ids):\n    time.sleep(20)\n    return {'n': 1}"
    with pytest.raises(ValueError, match="time limit"):
        review_control.reproduce(LOCAL, source, {}, ["n"])


def test_reproduction_driver_reports_execution_and_runner_errors(trusted_driver, monkeypatch):
    with pytest.raises(ValueError, match="arithmetic failed"):
        review_control.reproduce(LOCAL, "def reproduce(ids): raise ValueError('arithmetic failed')", {}, ["score"])

    def missing(*args, **kwargs):
        raise FileNotFoundError("missing runtime")

    monkeypatch.setattr(review_control.subprocess, "Popen", missing)
    with pytest.raises(ValueError, match="Could not start"):
        review_control.reproduce(LOCAL, "def reproduce(ids): return {'score': 0}", {}, ["score"])
