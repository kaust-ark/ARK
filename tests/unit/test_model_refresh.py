"""Installed timers and admin scripts can still call the retired source updater."""

from pathlib import Path
import runpy

import pytest


@pytest.mark.parametrize("flags", [[], ["--check"], ["--apply"], ["--notify"], ["--apply", "--notify"]])
def test_old_refresh_commands_only_print_the_catalog_notice(flags, monkeypatch, capsys):
    script = Path(__file__).resolve().parents[2] / "scripts" / "refresh_model_versions.py"
    main = runpy.run_path(str(script))["main"]

    def unexpected_side_effect(*args, **kwargs):
        pytest.fail("legacy refresh attempted a network connection or a file write")

    monkeypatch.setattr("socket.socket.connect", unexpected_side_effect)
    monkeypatch.setattr(Path, "write_text", unexpected_side_effect)
    assert main(flags) == 0
    assert "refreshes the model catalog automatically" in capsys.readouterr().out
