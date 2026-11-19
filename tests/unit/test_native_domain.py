"""Exercise scientific state through the native tool entry point."""

import hashlib
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest
import yaml

from ark.orchestrator import native_domain as domain


def open_project(root, monkeypatch, host=None):
    root.mkdir(exist_ok=True)
    (root / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "idea": "Compare two deterministic search algorithms",
                "venue_format": "icml",
                "layout_mode": "strict",
                "skip_deep_research": True,
                "figure_generation": "none",
            }
        )
    )
    project = domain.NativeProject(root, host=host)
    monkeypatch.setattr("ark.ethical_review.review_idea", lambda *args: {"verdict": "proceed"})
    project.invoke("project_status", {})
    return project


def plan(expected="results/count.json"):
    return {
        "question": "How many probes?",
        "baseline": "linear",
        "comparison": "binary",
        "metric": "comparisons",
        "script": "scripts/count.py",
        "expected": expected,
        "controls": ["Same sorted input and target"],
    }


def test_inspect_charges_nothing_and_creates_nothing(tmp_path, monkeypatch):
    project = domain.NativeProject(tmp_path)
    monkeypatch.setattr(project, "_initial_gate", lambda: pytest.fail("observing started a model turn"))
    assert project.invoke("inspect_project", {}) == {"state": "new", "delivered": False}
    assert list(tmp_path.iterdir()) == []


def test_status_rechecks_interrupted_ethics_and_explicit_resume_respects_refusal(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    assert project.state.pop("ethics_completed")
    project.save()
    (project.state_dir / "ethical_review.json").write_text('{"verdict":"decline"}')
    with pytest.raises(RuntimeError, match="idea review"):
        domain.NativeProject(tmp_path).invoke("project_status", {})
    reopened = domain.NativeProject(tmp_path)
    assert reopened.invoke("stop", {})["state"] == "stopped"
    assert reopened.invoke("continue_project", {})["state"] == "paused"
    with pytest.raises(ValueError, match="paused"):
        reopened.invoke("plan_experiment", plan())


def test_user_can_change_plan_before_a_side_effect(tmp_path, monkeypatch):
    seen = []

    def ask(summary):
        seen.append(summary)
        return "Test a missing target too" if len(seen) == 1 else None

    host = SimpleNamespace(
        _run_ethical_review=lambda: True,
        _poll_control=lambda: None,
        _experiment_approval_gate=ask,
        log=lambda *args: None,
    )
    project = open_project(tmp_path, monkeypatch, host)
    assert not project.invoke("plan_experiment", plan())["approved"]
    assert "Same sorted input" in seen[0]
    assert not project.invoke("plan_experiment", plan())["approved"]
    with pytest.raises(ValueError, match="requested change"):
        project.invoke("run_experiment", {})
    revised = plan()
    revised["controls"].append("Also evaluate an absent target")
    assert project.invoke("plan_experiment", revised) == revised
    assert len(seen) == 2


def test_explicit_stop_before_cancel_and_same_plan_recovery(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    project.invoke("plan_experiment", plan())
    project.state["sealed_script"] = "a" * 64
    project.save()
    states = []
    monkeypatch.setattr(
        domain.native_experiment,
        "cancel",
        lambda root: states.append(json.loads(project.path.read_text())["state"]),
    )
    cancelled = {"status": "cancelled", "job_id": "42"}
    monkeypatch.setattr(domain.native_experiment, "measurement_receipt", lambda *args: cancelled)
    assert project.invoke("stop", {})["state"] == "stopped"
    with pytest.raises(ValueError, match="paused"):
        project.invoke("run_experiment", {})
    assert domain.NativeProject(tmp_path).invoke("inspect_project", {})["state"] == "stopped"
    assert project.invoke("continue_project", {})["experiment"] == cancelled
    assert states == ["stopped", "stopped"]
    project.invoke("plan_experiment", plan("results/revised.json"))
    assert not project.state.get("sealed_script")


def test_source_evidence_requires_matching_identity_and_actual_passage(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    entry = {"key": "counts", "fields": {"doi": "10.1234/count", "title": "Counting rank comparisons"}}
    monkeypatch.setattr(domain.citation, "_search_arxiv", lambda *a, **kw: [])
    monkeypatch.setattr(
        domain.citation,
        "_http_get_json",
        lambda *a: {
            "message": {"DOI": "10.1234/count", "title": ["Entirely different work"], "abstract": "Unrelated"}
        },
    )
    assert project._source_record(entry) is None
    monkeypatch.setattr(
        domain.citation,
        "_http_get_json",
        lambda *a: {
            "message": {
                "DOI": "10.1234/count",
                "title": ["Counting rank comparisons"],
                "abstract": "<p>We counted queries.</p>",
            }
        },
    )
    source = json.loads((tmp_path / project._source_record(entry)).read_text())
    assert source["passage"] == "We counted queries."
    assert source["source"].startswith("Publisher-deposited abstract")


@pytest.mark.parametrize(("error", "pause_reason"), [
    ({"code": "PI_AI_ERROR", "message": "402 <!DOCTYPE HTML> Frozen qualification request cap."}, "quota"),
    ({"code": "PI_AI_ERROR", "message": "HTTP/1.1 402 Payment Required"}, "quota"),
    ({"code": "PI_AI_ERROR", "message": "402 Payment Required"}, "quota"),
    ({"status": 402, "message": "Provider rejected the request"}, "quota"),
    ({"code": "PI_AI_ERROR", "message": "Parse failure near token 402"}, "owner_error"),
    ({"code": "PI_AI_ERROR", "message": "402 tokens could not be decoded"}, "owner_error"),
    ({"code": "PI_AI_ERROR", "message": "4020: malformed input"}, "owner_error"),
    ({"status": 500, "message": "402 <!DOCTYPE HTML> quoted in upstream error"}, "owner_error"),
])
def test_owner_provider_exit_checkpoints_and_resumes_same_project(tmp_path, monkeypatch, error, pause_reason):
    project = open_project(tmp_path, monkeypatch)
    final = {"kind": "turn/end", "role": "owner", "session_id": "persistent", "seq": 224,
             "reason": {"kind": "error", "error": error}}
    project.invoke("project_event", final)
    fallback = {"kind": "failure", "role": "owner", "session_id": "persistent"}
    assert project.invoke("project_event", fallback) == {"recorded": False}
    reopened = domain.NativeProject(tmp_path)
    status = reopened.invoke("inspect_project", {})
    assert (status["state"], status["pause_reason"]) == ("paused", pause_reason)
    assert reopened.invoke("continue_project", {})["state"] == "running"
    assert reopened.invoke("project_event", {**final, "replayed": True}) == {"duplicate": True}
    assert reopened.state["session_seq"]["persistent"] == 224


def test_provider_setup_402_uses_existing_host_quota_handler(tmp_path, monkeypatch):
    observed = []
    host = SimpleNamespace(_run_ethical_review=lambda: True, _poll_control=lambda: None,
                           _record_native_event=observed.append, log=lambda *args: None)
    project = open_project(tmp_path, monkeypatch, host)
    event = {"kind": "failure", "role": "owner", "reason": {"kind": "error", "error": {"status": 402}}}
    project.invoke("project_event", event)
    assert project.state["pause_reason"] == "quota"
    assert observed == [{**event, "kind": "turn/end"}]
    project.invoke("project_event", event)
    assert len(observed) == 1


def test_pre_turn_owner_failure_is_recoverable_and_reviewer_failure_stays_local(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    error = {"kind": "turn/end", "reason": {"kind": "error", "error": {"message": "Malformed stream"}}}
    project.invoke("project_event", {**error, "role": "reviewer"})
    project.invoke("project_event", {**error, "role": "owner", "replayed": True})
    assert project.state["state"] == "running"
    project.invoke("project_event", {"kind": "failure", "role": "owner", "session_id": "persistent"})
    reopened = domain.NativeProject(tmp_path)
    assert reopened.state["pause_reason"] == "owner_error"
    assert reopened.invoke("continue_project", {})["state"] == "running"
    assert reopened.invoke("stop", {})["state"] == "stopped"
    reopened.invoke("project_event", {"kind": "failure", "role": "owner"})
    assert reopened.state["state"] == "stopped"


def test_quota_pause_sequence_dedup_and_resume(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    event = {"kind": "quota", "session_id": "s", "seq": 3, "usage": {"input_tokens": 12}}
    project.invoke("project_event", event)
    assert project.invoke("project_event", event) == {"duplicate": True}
    assert project.state["token_usage"]["input_tokens"] == 12
    assert project.invoke("inspect_project", {})["state"] == "paused"
    assert project.invoke("continue_project", {})["state"] == "running"


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="requires TeX Live")
def test_actual_compile_fingerprint_and_stale_delivery(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    source = project.latex_dir / "main.tex"
    source.write_text(r"\documentclass{article}\begin{document}Search comparisons.\end{document}")
    report = project.invoke("compile_paper", {})
    assert report["ok"] and report["pages"] == 1 and "main.tex" in report["inputs"]
    pdf = project.latex_dir / "main.pdf"
    assert report["pdf_sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert project.invoke("compile_paper", {}) == report
    assert not project.invoke("deliver", {})["delivered"]
    project.state.update(state="delivered", delivery_revision="old")
    project.save()
    source.write_text(source.read_text().replace("comparisons", "results"))
    assert project.invoke("inspect_project", {})["state"] == "running"
    assert project.state["state"] == "delivered"  # read-only observer
    assert project.invoke("project_status", {})["state"] == "running"
    with pytest.raises(ValueError, match="Compile"):
        project.invoke("review_snapshot", {})


def test_transport_carries_only_requested_result(tmp_path):
    reader = io.StringIO('{"id":"bad","op":"eval","args":{}}\n{"id":"read","op":"inspect_project"}\n')
    writer = io.StringIO()
    domain.serve(domain.NativeProject(tmp_path), reader, writer)
    bad, good = [json.loads(s) for s in writer.getvalue().splitlines()]
    assert not bad["ok"] and bad["id"] == "bad"
    assert good["ok"] and good["result"]["state"] == "new"


def test_primary_source_resolution_requires_correct_identity(monkeypatch):
    evidence = domain.literature_evidence
    entry = {
        "key": "demo",
        "fields": {"author": "Ada Lovelace", "year": "2020", "title": "Comparing rank queries"},
    }
    record = {
        "id": "https://openalex.org/W100",
        "display_name": "Comparing rank queries",
        "doi": None,
        "publication_year": 2020,
        "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
        "locations": [{"pdf_url": "https://arxiv.org/pdf/2001.00001"}],
        "abstract_inverted_index": {"Comparisons": [0], "help": [1]},
    }
    monkeypatch.setattr(evidence.citation, "_http_get_json", lambda *args: {"results": [record]})
    monkeypatch.setattr(evidence, "_paper_excerpt", lambda url: ["Measured rank errors. " * 30])
    source = evidence.resolve(entry)
    assert source["url"].startswith("https://arxiv.org/pdf/") and "[Page 1]" in source["passage"]
    record["locations"] = [{"pdf_url": "https://invalid.example.test/unsafe.pdf"}]
    assert evidence.resolve(entry)["passage"] == "Comparisons help"
    record["authorships"] = [{"author": {"display_name": "Another Researcher"}}]
    assert evidence.resolve(entry) is None


def test_reviewer_reservation_survives_restart_and_is_bound_to_revision(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    project.config["max_review_attempts"] = 1
    project.latex_dir.joinpath("main.pdf").write_bytes(b"temporary snapshot")
    project.state["compiled_pdf_sha256"] = hashlib.sha256(b"temporary snapshot").hexdigest()
    monkeypatch.setattr(project, "_check_compiled", lambda: None)
    monkeypatch.setattr(project, "_snapshot", lambda: {"revision": "rev1", "artifacts": {}})
    monkeypatch.setattr(domain.review, "review_prompt", lambda *args, **kw: "Inspect evidence.")
    project.review_snapshot({"session_id": "child1"})
    assert json.loads(project.path.read_text())["review_attempts"] == {"child1": "rev1"}
    project.review_snapshot({"session_id": "child1"})
    with pytest.raises(ValueError, match="budget exhausted"):
        project.review_snapshot({"session_id": "child2"})


def test_measurement_table_uses_worker_decimal_values_even_beyond_model_excerpt(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    results = tmp_path / "results/paired.json"
    from decimal import Decimal

    count = 40
    data = {
        "pairs": [
            {"seed": i, "arm": {"loss": float(Decimal(i) / 800)}, "note": "x" * 100} for i in range(count)
        ]
    }
    results.write_text(json.dumps(data))
    project.state["experiment"] = {
        "status": "completed",
        "artifact": "results/paired.json",
        "sha256": hashlib.sha256(results.read_bytes()).hexdigest(),
    }
    choices = project.invoke("render_results_table", {})["available"]
    assert choices[0]["rows"] == count and "arm.loss" in choices[0]["columns"]
    args = {
        "name": "comparison",
        "rows_pointer": "/pairs",
        "columns": [
            {"path": "seed", "digits": 0},
            {"path": "arm.loss", "label": "Loss %", "digits": 3, "percent": True},
        ],
    }
    r = project.invoke("render_results_table", args)
    contents = (project.latex_dir / r["path"]).read_text()
    assert r"Loss \%" in contents and "39 & 4.875" in contents
    main = project.latex_dir / "main.tex"
    main.write_text(r"\input{sections/results}\end{document}")
    (project.latex_dir / "sections").mkdir()
    section = project.latex_dir / "sections/results.tex"
    section.write_text(r"\input{" + r["path"] + "}")
    project._restore_measured_table()
    assert "table*" not in main.read_text()
    section.write_text("% The old table was removed")
    project._restore_measured_table()
    project._restore_measured_table()
    assert main.read_text().count(r"\begin{table*}") == 1
    results.write_text('{"pairs":[]}')
    with pytest.raises(ValueError, match="worker receipt"):
        project.invoke("render_results_table", args)


def test_detects_named_optimizer_absent_from_worker(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    script = tmp_path / "scripts/fit.py"
    script.write_text("from scipy.special import logsumexp as lse\nx=lse([1,2])\n")
    project.state.update(
        plan={"script": "scripts/fit.py"},
        experiment={"status": "completed", "code_sha256": hashlib.sha256(script.read_bytes()).hexdigest()},
    )
    tex = project.latex_dir / "main.tex"
    tex.write_text(
        r"We used \texttt{scipy.special.logsumexp}. Fit using the \texttt{scipy.optimize.minimize} routine."
    )
    assert len(project._implementation_claims()) == 1
    assert "scipy.optimize.minimize" in project._implementation_claims()[0]
    tex.write_text(r"We used \texttt{sklearn.model\_selection.GridSearchCV}.")
    assert "sklearn.model_selection.GridSearchCV" in project._implementation_claims()[0]

@pytest.mark.skipif(not shutil.which("pdflatex"), reason="requires TeX Live")
def test_failed_new_build_survives_resume_without_reusing_old_review(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    main = project.latex_dir / "main.tex"
    main.write_text(r"\documentclass{article}\begin{document}First version.\end{document}")
    assert project.invoke("compile_paper", {})["ok"]
    main.write_text(r"\documentclass{article}\begin{document}We used \texttt{metric_value}.\end{document}")
    error = project.invoke("compile_paper", {})
    assert not error["ok"] and "Missing $" in error["diagnostic"] and "suggested_edit" in error["diagnostic"]
    reopened = domain.NativeProject(tmp_path)
    assert reopened.invoke("project_status", {})["manuscript_issues"] == [error["diagnostic"]]
    assert "compiled_pdf_sha256" not in reopened.state
    with pytest.raises(ValueError, match="Compile"):
        reopened.invoke("review_snapshot", {})
    assert reopened.invoke("compile_paper", {}) == error
    events = (project.state_dir / "research_events.jsonl").read_text()
    assert events.count('"action": "paper_compile_failed"') == 1
    edit = error["suggested_edit"]
    assert edit == {"path":"paper/main.tex", "replace":r"\texttt{metric_value}", "content":r"\texttt{metric\_value}","expected_matches":1}
    main.write_text(main.read_text().replace(edit["replace"], edit["content"]))
    assert reopened.invoke("compile_paper", {})["ok"]


def test_verified_concept_can_be_reused_without_charging_or_loading_a_key(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    project.state["plan"] = plan()
    image = project.figures_dir / "overview.png"
    image.write_bytes(b"frozen figure fixture")
    sha = hashlib.sha256(image.read_bytes()).hexdigest()
    rec = {"path":"paper/figures/overview.png", "semantic_pass":True, "sha256":sha}
    project.state["concept"] = rec
    (project.state_dir / "native_review.json").write_text(json.dumps({"artifact_hashes":{rec["path"]:"old"}}))
    monkeypatch.setattr(domain.figure_manifest,"load_manifest",lambda *a, **kw: {"figures":{
        image.name:{"artifact_sha256":sha,"source":"nano_banana","provider":"google"}}})
    monkeypatch.setattr(domain.deep_research,"get_gemini_api_key",lambda: None)
    args = {"name":"overview","caption":"Protocol","description":"Show the paired comparison"}
    assert project.invoke("generate_concept_figure",args)["already_revised"]
    image.write_bytes(b"unverified replacement")
    with pytest.raises(RuntimeError,match="Google key"):
        project.invoke("generate_concept_figure",args)

def test_fenced_source_repair_preserves_code_and_ordinary_tex(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    issue = "./main.tex:3: Missing $ inserted"
    main = project.latex_dir / "main.tex"
    main.write_text("Results\n```python\nscore_per_seed = [4, 6]\n```\nContinue.")
    edit = project._suggest_tex_edit(project.latex_dir, issue)
    assert edit["expected_matches"] == 1 and edit["path"] == "paper/main.tex"
    assert edit["content"].splitlines() == [r"\begin{verbatim}", "score_per_seed = [4, 6]", r"\end{verbatim}"]
    main.write_text("x\ny\nz\n")
    assert project._suggest_tex_edit(project.latex_dir, issue) is None
