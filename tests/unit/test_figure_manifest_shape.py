"""load_manifest must tolerate agent-written manifests of the wrong shape.

An experimenter agent wrote figure_manifest.json with ``figures`` as a LIST
of {file, width, placement} entries; consumers index it as a dict and a live
run crashed with AttributeError (project 3aaae35b, 2026-07-08).
"""

import json
from pathlib import Path

from ark.figure_manifest import load_manifest


def _write(tmp_path: Path, payload) -> Path:
    (tmp_path / "figure_manifest.json").write_text(json.dumps(payload))
    return tmp_path


def test_list_shaped_figures_coerced_to_dict(tmp_path):
    _write(tmp_path, {
        "generated": "2026-07-08",
        "figures": [
            {"file": "fig1.pdf", "width": "columnwidth", "placement": "t"},
            {"file": "fig2.pdf", "width": "columnwidth"},
            {"no_file_key": True},
        ],
    })
    m = load_manifest(tmp_path)
    figs = m["figures"]
    assert isinstance(figs, dict)
    assert figs["fig1.pdf"]["placement"] == "t"
    assert figs["fig2.pdf"]["width"] == "columnwidth"
    assert len(figs) == 2  # entry without "file" is dropped
    # The crash pattern must work now:
    assert figs.get("fig1.pdf", {}).get("source") is None


def test_non_dict_figures_reset_to_empty(tmp_path):
    _write(tmp_path, {"figures": "garbage"})
    assert load_manifest(tmp_path)["figures"] == {}


def test_non_dict_toplevel_falls_back_to_rebuild(tmp_path):
    _write(tmp_path, ["not", "a", "manifest"])
    m = load_manifest(tmp_path)
    assert isinstance(m, dict)
    assert isinstance(m.get("figures", {}), dict)


def test_valid_dict_manifest_passthrough(tmp_path):
    _write(tmp_path, {"figures": {"a.pdf": {"source": "matplotlib"}}})
    m = load_manifest(tmp_path)
    assert m["figures"]["a.pdf"]["source"] == "matplotlib"
