"""Orchestrator live-log push glue (Phase 1, step 4).

Exercises the real Orchestrator._push_event / _flush_events against a fake client,
bypassing __init__ (which needs the full research stack). Skipped on <3.10 because
ark.orchestrator.core uses 3.10+ typing syntax at import.
"""

import sys
import threading

import pytest

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 10),
    reason="ark.orchestrator.core uses 3.10+ typing syntax at import",
)


class _FakeCP:
    def __init__(self, emits):
        self._emits = emits
        self.sent = []

    @property
    def emits_events(self):
        return self._emits

    def append_events(self, lines):
        self.sent.extend(lines)


def _bare_orchestrator(cp):
    from ark.orchestrator.core import Orchestrator
    o = Orchestrator.__new__(Orchestrator)  # bypass heavy __init__
    o.cp = cp
    o._event_buf = []
    o._event_lock = threading.Lock()
    o._event_flusher = None
    o._stop_requested = True  # flusher loop exits immediately; we flush manually
    return o


def test_push_and_flush_over_emitting_transport():
    cp = _FakeCP(emits=True)
    o = _bare_orchestrator(cp)
    o._push_event("alpha")
    o._push_event("beta")
    o._flush_events()
    assert [e["line"] for e in cp.sent] == ["alpha", "beta"]
    assert all("ts" in e for e in cp.sent)
    o._flush_events()  # buffer empty → no-op
    assert len(cp.sent) == 2


def test_push_is_noop_when_transport_has_shared_fs():
    cp = _FakeCP(emits=False)
    o = _bare_orchestrator(cp)
    o._push_event("nope")
    assert o._event_buf == []
    o._flush_events()
    assert cp.sent == []
