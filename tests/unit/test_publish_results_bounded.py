"""The end-of-run results publish is bounded and narrates its progress.

a247437e (2026-09-17) finished its paper, then spent three silent hours
registering 78,719 of its 650,215 result files with the control plane over
NFS; the stuck-run watchdog read the silence as a wedged run, killed it, and
filed the finished paper as failed. Every call now stops at a file, byte or
time budget, says so once, and logs progress so the run stays visibly alive.
"""
from ark.artifacts import ArtifactRef
from ark.artifacts.publish import publish_result_artifacts


class _Store:
    def put_path(self, src, key, *, content_type=""):
        return ArtifactRef("local", key, content_type, src.stat().st_size, "00")


class _CP:
    def __init__(self):
        self.keys = []

    def upload_artifact(self, key, data, *, kind="", content_type=""):
        self.keys.append(key)

    def register_artifact(self, **ref):
        self.keys.append(ref["key"])


def _results(tmp_path, n, size=100, ext=".json", sub=""):
    root = tmp_path / "results" / sub
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (root / f"f{i:03d}{ext}").write_bytes(b"x" * size)
    return tmp_path


def test_file_cap_publishes_the_first_files_and_says_so(tmp_path):
    code = _results(tmp_path, 12)
    cp, logs = _CP(), []
    n = publish_result_artifacts(_Store(), cp, code, log=lambda m, lvl="": logs.append(m),
                                 max_files=5)
    assert n == 5
    assert cp.keys == [f"results/f{i:03d}.json" for i in range(5)]
    stops = [m for m in logs if "results publish stopped" in m]
    assert len(stops) == 1 and "(file cap)" in stops[0] and "results/" in stops[0]


def test_byte_cap_counts_only_what_was_published(tmp_path):
    code = _results(tmp_path, 10, size=100)
    cp, logs = _CP(), []
    n = publish_result_artifacts(_Store(), cp, code, log=lambda m, lvl="": logs.append(m),
                                 max_total_bytes=250)
    assert n == 3                       # 100 + 100 + 100 >= 250 → stop
    assert any("(byte cap)" in m for m in logs)


def test_time_budget_bounds_even_the_walk(tmp_path):
    code = _results(tmp_path, 10)
    cp, logs = _CP(), []
    n = publish_result_artifacts(_Store(), cp, code, log=lambda m, lvl="": logs.append(m),
                                 time_budget_s=0)
    assert n == 0 and cp.keys == []
    assert any("(time budget)" in m for m in logs)


def test_progress_is_logged_while_publishing(tmp_path):
    code = _results(tmp_path, 4)
    cp, logs = _CP(), []
    n = publish_result_artifacts(_Store(), cp, code, log=lambda m, lvl="": logs.append(m),
                                 progress_every_s=0)
    assert n == 4
    assert any(m.startswith("results publish:") and "seen" in m for m in logs)
    assert not any("stopped" in m for m in logs)


def test_ineligible_files_do_not_consume_the_file_cap(tmp_path):
    code = _results(tmp_path, 3, ext=".bin")
    _results(tmp_path, 3, ext=".json")
    cp = _CP()
    assert publish_result_artifacts(_Store(), cp, code, max_files=3) == 3
    assert all(k.endswith(".json") for k in cp.keys)


def test_walk_is_sorted_and_top_level_first(tmp_path):
    code = _results(tmp_path, 1, sub="deep")       # results/deep/f000.json
    (tmp_path / "results" / "a.json").write_bytes(b"x")
    (tmp_path / "results" / "z.json").write_bytes(b"x")
    cp = _CP()
    assert publish_result_artifacts(_Store(), cp, code, max_files=2) == 2
    assert cp.keys == ["results/a.json", "results/z.json"]


def test_defaults_are_generous_but_finite():
    from ark.artifacts import publish as p
    assert 1000 <= p._RESULT_MAX_FILES <= 50_000
    assert 60 <= p._RESULT_TIME_BUDGET_S <= 1800
    assert p._RESULT_PROGRESS_EVERY_S <= 120
