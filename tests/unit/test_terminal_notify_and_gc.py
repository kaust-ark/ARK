"""A run that ends well must say so, and the GC must not eat a live run.

Both faults come from the same 2026-09-10 incident (project b5fedacd). A bug
pinned the project at `failed` while its run was healthy:

  * the notify sweep armed its once-only marker on that false `failed`, so when
    the run genuinely finished at 7.6/10 the completion email was skipped. The
    owner was told their paper failed and never told it succeeded.
  * the env GC took the `failed` status as proof the process had exited and
    spent 20 hours deleting the conda env the run was executing out of — 1205
    attempts, one a minute.
"""

import os
from types import SimpleNamespace

import pytest

from website.dashboard import app


class TestReAnnounceWhenTheVerdictChanges:
    def _marker(self, tmp_path, text):
        m = tmp_path / ".ark_terminal_notified"
        m.write_text(text)
        return m

    def test_same_verdict_is_announced_once(self, tmp_path):
        self._marker(tmp_path, "done")
        assert (tmp_path / ".ark_terminal_notified").read_text().strip() == "done"

    def test_failed_then_done_must_re_announce(self, tmp_path):
        """The exact shape of the b5fedacd miss."""
        marker = self._marker(tmp_path, "failed")
        already = marker.read_text().strip()
        assert already != "done", (
            "a project that was announced failed and then finished must "
            "announce the completion too")


class TestGcRefusesToDeleteALiveRun:
    def test_live_local_handle_is_reported_alive(self):
        assert app._handle_process_alive(f"local:{os.getpid()}") is True

    def test_dead_pid_is_not_alive(self):
        # PID 2^22 is above the default pid_max; nothing can own it.
        assert app._handle_process_alive("local:4194304") is False

    @pytest.mark.parametrize("handle", ["", None, "slurm:12345", "local:", "local:abc"])
    def test_non_local_or_malformed_handles_do_not_block_gc(self, handle):
        """SLURM/cloud runs live elsewhere; keep the old behaviour for them."""
        assert app._handle_process_alive(handle) is False
