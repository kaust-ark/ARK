"""Security regression: venue_format must not escape the templates root.

``venue_format`` is user-controlled on ``POST /api/projects`` (and the restart
flow). It is only ever a bundled template *directory name*. Before the fix,
``_TEMPLATES_ROOT / venue_format`` with an absolute path (``/etc``) or a parent
traversal (``../..``) resolved outside the templates tree, letting a caller copy
arbitrary server files into the project's ``paper/`` dir and exfiltrate them via
``GET /api/projects/{id}/zip`` (path traversal / arbitrary file read).
"""

from pathlib import Path

import pytest

from website.dashboard import templates as t


@pytest.mark.parametrize(
    "payload",
    [
        "/etc",           # absolute path — the classic `root / "/etc"` collapse
        "/etc/",          # absolute path with trailing slash
        "../../etc",      # parent traversal
        "..",             # bare parent ref
        "foo/bar",        # any separator escapes the flat name space
        "foo\\bar",       # backslash separator (defense in depth)
        "",               # empty
    ],
)
def test_traversal_payloads_are_rejected(payload):
    assert t.has_venue_template(payload) is False
    assert t._resolved_template_dir(payload) is None


def test_traversal_copy_is_a_noop(tmp_path):
    dest = tmp_path / "paper"
    copied = t.copy_venue_template("/etc", dest)
    assert copied is False
    # Nothing from /etc should have been written into the project dir.
    leaked = list(dest.glob("*")) if dest.exists() else []
    assert leaked == []


def test_legitimate_venue_still_resolves():
    # A real bundled venue and a legacy alias must keep working.
    assert t.has_venue_template("neurips") is True
    assert t.has_venue_template("emnlp") is True  # alias -> acl
    resolved = t._resolved_template_dir("neurips")
    assert resolved is not None
    assert resolved.parent == Path(t._TEMPLATES_ROOT).resolve()


def test_legitimate_venue_copies_files(tmp_path):
    dest = tmp_path / "paper"
    assert t.copy_venue_template("neurips", dest) is True
    assert list(dest.glob("*")), "expected the neurips skeleton to be copied"
