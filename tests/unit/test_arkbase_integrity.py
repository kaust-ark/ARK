"""diff_state: the shared-env watchdog's baseline comparison."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_arkbase_integrity",
    Path(__file__).resolve().parents[2] / "scripts" / "check_arkbase_integrity.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
diff_state = _mod.diff_state


def test_clean_when_identical():
    s = {"sqlalchemy": "2.0.51", "litellm": "1.74.0"}
    assert diff_state(s, dict(s)) == []


def test_downgrade_reported_with_both_versions():
    drift = diff_state({"sqlalchemy": "2.0.51"}, {"sqlalchemy": "1.3.22"})
    assert drift == ["sqlalchemy: 2.0.51 -> 1.3.22"]


def test_removal_reported_as_absent():
    drift = diff_state({"litellm": "1.74.0"}, {"litellm": None})
    assert drift == ["litellm: 1.74.0 -> absent"]


def test_new_package_flagged():
    drift = diff_state({}, {"otree": "6.0.15"})
    assert drift == ["otree: (not in baseline) -> 6.0.15"]
