"""Result numbers in the paper must come from the project's result files.

Shapes from production: b5fedacd's 7.4 draft printed a synthetic fallback's
constants while results/ was still empty; 5c0e44f1's 6.7 draft printed model
accuracies no query had produced. Verified final papers traced 96-100%.
"""
import json
import os

from ark.provenance import audit, claims, verdict

PAPER = r"""\begin{document}
\begin{abstract}
Our router reaches 82.31\% accuracy, 7.42 points above the baseline (74.89\%).
\end{abstract}
\section{Results}
On the held-out split the mean error is 0.1234 (std 0.0456), and recall is 0.9012.
\begin{table}[t]\begin{tabular}{lr}
Router & 82.31 \\ Baseline & 74.89 \\ Oracle & 91.07 \\
\end{tabular}\caption{Accuracy (see~\cite{x}).}\label{tab:a}\end{table}
\section{Related Work}
Earlier systems scored 55.5 on a different benchmark.
\end{document}
"""


def _project(tmp_path, values=None):
    (tmp_path / "results").mkdir()
    if values is not None:
        (tmp_path / "results" / "summary.json").write_text(json.dumps(values))
    return tmp_path


REAL = {"router_acc": 0.82312, "baseline_acc": 0.74889, "oracle": 91.07,
        "gap": 7.423, "mean_err": 0.12341, "std_err": 0.04558, "recall": 0.90119}


def test_numbers_come_from_results_sections_tables_and_abstract_only():
    nums = [c[1] for c in claims(PAPER)]
    assert "82.31" in nums and "0.1234" in nums and "91.07" in nums
    assert "55.5" not in nums          # Related Work is not a result section


def test_a_paper_whose_numbers_are_in_results_passes(tmp_path):
    r = audit(PAPER, _project(tmp_path, REAL))
    assert r["ratio"] == 1.0
    assert verdict(r)[0]


def test_numbers_with_no_result_files_at_all_fail(tmp_path):
    padded = PAPER.replace("0.9012.", "0.9012, 0.1111, 0.2222, 0.3333, 0.4444.")
    r = audit(padded, _project(tmp_path))
    ok, detail = verdict(r)
    assert not ok and "hold no readable values" in detail


def test_invented_numbers_next_to_unrelated_results_fail(tmp_path):
    r = audit(PAPER, _project(tmp_path, {"loss": [3.141, 2.718, 1.414], "steps": 1000}))
    ok, detail = verdict(r)
    assert not ok and "82.31" in detail


def test_files_newer_than_the_draft_are_not_evidence(tmp_path):
    root = _project(tmp_path, REAL)
    os.utime(root / "results" / "summary.json", (2_000_000_000, 2_000_000_000))
    assert audit(PAPER, root, before=1_900_000_000)["evidence_values"] == 0
    assert audit(PAPER, root)["ratio"] == 1.0


def test_a_paper_without_numbers_has_nothing_to_trace(tmp_path):
    ok, detail = verdict(audit(r"\section{Results}A taxonomy, no measurements.", _project(tmp_path)))
    assert ok and "no result numbers" in detail


def test_the_walk_over_results_is_bounded(tmp_path):
    root = _project(tmp_path)
    deep = root / "results" / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "x.json").write_text(json.dumps(REAL))     # below max_depth: not read
    for i in range(450):
        (root / "results" / f"f{i:03d}.txt").write_text("1")
    r = audit(PAPER, root)
    assert r["strong_traced"] == 0      # neither the deep file nor file #401+ was read


def test_the_review_time_audit_is_kept_for_delivery(tmp_path):
    from ark.pipeline import PipelineMixin
    root = _project(tmp_path)          # results/ empty, as when b5fe was reviewed
    (root / "auto_research" / "state").mkdir(parents=True)
    p = PipelineMixin.__new__(PipelineMixin)
    p.code_dir, p.state_dir, p.iteration, p.logs = root, root / "auto_research" / "state", 1, []
    p.log = lambda m, l="INFO": p.logs.append((l, m))
    p._audit_numbers_at_review(PAPER.replace("0.9012.", "0.9012, 0.1111, 0.2222, 0.3333, 0.4444."))
    rec = json.loads((p.state_dir / "provenance_at_review.json").read_text())
    assert rec["ok"] is False and rec["iteration"] == 1
    assert any(l == "WARN" and "provenance" in m for l, m in p.logs)
