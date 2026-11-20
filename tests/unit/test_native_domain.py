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


def test_cli_progress_reports_persisted_observations_on_stderr(tmp_path, monkeypatch, capsys):
    project = open_project(tmp_path, monkeypatch)
    capsys.readouterr()
    project.event("writing", "paper_compiled", "Rendered six pages.", artifact="paper/main.pdf")
    project.event("work", "tool/result", "owner tool returned")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == ["[writing] Rendered six pages. (paper/main.pdf)"]


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="requires TeX Live")
def test_error_bar_caption_requires_the_executed_renderer_and_the_printed_image(tmp_path, monkeypatch):
    from matplotlib.figure import Figure
    from PIL import Image

    project = open_project(tmp_path, monkeypatch)
    name = "response.png"
    image = project.figures_dir / name
    data = [{"seed": 1, "loss": 3.5}, {"seed": 2, "loss": 2.9}]
    spec = domain.measurement_table.plot_spec(data, {"kind": "bar", "x": {"path": "seed"}, "series": [{"path": "loss"}]})
    renderer = domain.native_experiment
    code = renderer.measured_plot_code(spec, name)
    # Execute the real fixed worker program locally. Redirect just its two container paths.
    real_open, savefig = open, Figure.savefig
    with monkeypatch.context() as worker:
        worker.setattr("builtins.open", lambda path, *a, **kw: io.StringIO(json.dumps(data))
                       if path == "/study/results.json" else real_open(path, *a, **kw))
        worker.setattr(Figure, "savefig", lambda fig, path, **kw: savefig(fig, image, **kw))
        exec(code, {})
    identity = "a" * 24
    row = dict(identity="i2p-" + identity, status="completed", script=None, plot_protocol=1,
               plot_filename=name, plot_spec=spec, input_access="worker_observed_read", artifact="paper/figures/" + name,
               sha256=domain._sha(image), code_sha256=hashlib.sha256(code).hexdigest())
    receipt = project.state_dir / "native_runs" / identity / "receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps(row))
    project.state["plots"] = {name: row}
    source = project.latex_dir / "main.tex"
    claim = "Error bars indicate variance across splits."
    source.write_text(r"""\documentclass{article}
\usepackage{graphicx}
\begin{document}
\begin{figure}[ht]\centering
\includegraphics[width=120pt]{figures/response.png}
\caption{Observed losses. Error bars indicate variance across splits.}\label{fig:result}
\end{figure}
\end{document}
""")
    compiled = project.compile_paper({})
    note, = project._plot_caption_issues()
    assert "Figure 1 (PDF p. 1, main.tex:6, response.png)" in note and claim[:-1] in note
    assert note in compiled["manuscript_issues"] and note in project.review_snapshot({"preflight": True})["blockers"]
    snapshot = project._snapshot()
    snapshot["measurement_reproduction"] = dict(artifact="results/count.json", rows_pointer="", identifiers={"seed": 1},
                                                columns=["loss"])
    monkeypatch.setattr(project, "_snapshot", lambda: snapshot)
    packet = project.review_snapshot({})
    assert note in packet["brief"] and "error bars" not in packet["reproduction_brief"].lower()

    original = source.read_text()
    for caption in ("No error bars are shown.", "The error bars would show variance across splits.",
                    "Panel (b). " + claim, "Error bars indicate variance across splits in earlier studies.",
                    claim + " None are shown here."):
        source.write_text(original.replace(claim, caption))
        assert project._plot_caption_issues() == []
    source.write_text(original.replace("\\caption{", r"\includegraphics[width=30pt]{figures/response.png}\caption{"))
    assert project._plot_caption_issues() == []
    source.write_text(original)
    with monkeypatch.context() as changed:
        changed.setattr(renderer, "MEASURED_PLOT", renderer.MEASURED_PLOT + "\n# unaudited version")
        assert project._plot_caption_issues() == []
    for delta in ({"script": "scripts/uncertainty.py"}, {"code_sha256": "0" * 64}):
        project.state["plots"][name] = {**row, **delta}
        receipt.write_text(json.dumps(project.state["plots"][name]))
        assert project._plot_caption_issues() == []
    project.state["plots"][name] = row
    assert project._plot_caption_issues() == []  # The durable receipt contradicts the project pointer.
    receipt.write_text(json.dumps(row))
    Image.new("RGB", (60, 40), "red").save(image)
    assert project._plot_caption_issues() == []
    row["sha256"] = domain._sha(image)
    receipt.write_text(json.dumps(row))
    assert project._plot_caption_issues() == []  # The PDF still embeds the original pixels.


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


def test_revising_a_completed_study_reports_matching_rows_to_author_and_reviewer(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    original = project.invoke("plan_experiment", plan())
    def result(name, rows):
        data = json.dumps({"splits": rows}).encode()
        (tmp_path / name).write_bytes(data)
        return {"artifact": name, "sha256": hashlib.sha256(data).hexdigest(), "status": "completed"}

    rows = [{"seed": 1, "loss": 0.75}, {"seed": 2, "loss": 1.25}]
    old = result(original["expected"], rows)
    project.state["experiment"] = old
    revised = {**original, "expected": "results/revised.json"}
    assert project.invoke("plan_experiment", revised)["previous_result"] == old["artifact"]
    new = result(revised["expected"], [rows[0], {"seed": 2, "loss": 1.25 + 1e-15}])
    new.update(backend="slurm", identity="i2p-" + "a" * 24, job_id="412")
    (tmp_path / revised["script"]).write_text("print('measurement')")
    monkeypatch.setattr(project, "_measurement_receipt", lambda: new)
    monkeypatch.setattr(domain.native_experiment, "execute", lambda *args, **kwargs: new)
    check = project.invoke("run_experiment", {})["revision_check"]
    assert check["unchanged_arrays"] == [{"rows_pointer": "/splits", "rows": 2}]
    events = [json.loads(row) for row in (project.state_dir / "research_events.jsonl").read_text().splitlines()]
    assert "2 result rows match results/count.json" in events[-1]["observation"]
    assert project.invoke("run_experiment", {})["revision_check"] == check
    assert len(events) == len((project.state_dir / "research_events.jsonl").read_text().splitlines())
    snap = project._snapshot()
    assert snap["measurement_comparison"] == check
    assert {old["artifact"], new["artifact"]} <= set(project._opening_review_evidence(snap))
    result(old["artifact"], [{"seed": 4, "loss": 50}])
    assert "measurement_comparison" not in project._snapshot()
    project._compare_revised_measurement(new)
    assert "measurement_comparison" not in project.state


def test_registered_contrast_follows_the_worker_into_writing_and_review(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    mechanism = {"explanation": "Later targets are inspected after more comparisons.",
                 "alternative": "Initialization sets the cost independently of target position.",
                 "distinguishing_observation": "Compare first and last target probe counts for each size.",
                 "claim_without_it": "Report the observed elapsed time only.",
                 "measurement": {"rows_pointer": "/runs~1table/0", "columns": ["probes.first", "probes.last"]}}
    planned = project.invoke("plan_experiment", {**plan(), "mechanism": mechanism})
    rows = [{"probes": {"first": 0, "last": 8}}, {"probes": {"first": 1, "last": None}}]
    result = tmp_path / planned["expected"]
    result.write_text(json.dumps({"runs/table": [rows]}))
    script = tmp_path / planned["script"]
    script.parent.mkdir(exist_ok=True)
    script.write_text("print('worker fixture')")
    receipt = {"status":"completed", "identity":"i2p-" + "1"*24, "backend":"slurm",
               "artifact": planned["expected"], "script":planned["script"], "code_sha256":domain._sha(script),
               "sha256":domain._sha(result), "plan_sha256":domain.native_experiment.plan_digest(planned)}
    monkeypatch.setattr(domain.native_experiment, "execute", lambda *a, **k: receipt)
    monkeypatch.setattr(project, "_measurement_receipt", lambda: receipt)
    context = project.invoke("run_experiment", {})["experiment_design"]
    assert context["observation"] == {"status":"partial", "count":1, "total":2}
    assert context == project._snapshot()["registered_design"]
    project.invoke("run_experiment", {})
    observed = [e for e in (json.loads(l) for l in (project.state_dir/"research_events.jsonl").read_text().splitlines())
                if e["action"] == "mechanism_observation"]
    assert len(observed) == 1 and observed[0]["count"] == 1
    reopened = domain.NativeProject(tmp_path)
    assert reopened.inspect_project({})["mechanism_observation"]["sha256"] == receipt["sha256"]
    receipt["plan_sha256"] = "0"*64
    assert "registered_design" not in project._snapshot()
    receipt["plan_sha256"] = domain.native_experiment.plan_digest(planned)
    result.write_text('{"runs/table":[[{"probes":{"first":1,"last":2}}]]}')
    assert "registered_design" not in project._snapshot()


def test_registered_numeric_columns_count_only_observed_finite_values():
    measure = domain.measurement_table
    spec = {"rows_pointer":"", "columns":["score"]}
    mechanism = {"measurement": measure.planned_columns(spec)}
    planned = {**plan(), "mechanism":mechanism}
    assert measure.experiment_design(planned, [{"score":1e-30}, {"score":0}])["observation"] == {
        "status":"recorded", "count":2, "total":2}
    assert measure.experiment_design(planned, [{"score": True}, {"score": "3"}, {"score": float("inf")}])["observation"] == {
        "status":"missing", "count":0, "total":3}
    assert measure.experiment_design(planned, [])["observation"] == {"status":"missing"}
    assert measure.experiment_design({**plan(), "mechanism":{"explanation":"legacy"}}, [])["observation"]["status"] == "unspecified"
    assert "observation" not in measure.experiment_design(plan(), [])
    for invalid in ({**spec, "rows_pointer":"counts"}, {**spec, "rows_pointer":"/a~2b"}, {**spec, "columns":[]},
                    {**spec, "columns":["s", "s"]}, {**spec, "columns":[".x"]}):
        with pytest.raises(ValueError):
            measure.planned_columns(invalid)


def test_unchanged_rows_preserve_identifiers_and_small_probabilities():
    from ark.measurement_table import unchanged_result_arrays

    old = [{"id": 10**18, "prob": 1e-300, "label": "test"}]
    for delta in ({"id": 10**18 + 1}, {"prob": 2e-300}, {"label": "train"}, {"prob": True}):
        assert not unchanged_result_arrays(old, [{**old[0], **delta}])


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
    assert report["manuscript_revision"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    pdf = project.latex_dir / "main.pdf"
    assert report["pdf_sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert project.invoke("compile_paper", {}) == report
    assert not project.invoke("project_status", {}).get("build_stale")
    assert not project.invoke("deliver", {})["delivered"]
    project.state.update(state="delivered", delivery_revision="old")
    project.save()
    source.write_text(source.read_text().replace("comparisons", "results"))
    assert project.invoke("inspect_project", {})["state"] == "running"
    assert project.state["state"] == "delivered"  # read-only observer
    resumed = project.invoke("project_status", {})
    assert resumed["state"] == "running" and resumed["build_stale"]
    assert "Run compile_paper" in resumed["manuscript_issues"][0]
    with pytest.raises(ValueError, match="Compile"):
        project.invoke("review_snapshot", {})


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="requires TeX Live")
def test_icml_first_edit_and_review_require_the_loaded_venue(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    main = project.latex_dir / "main.tex"
    stock = main.read_text()
    plain = r"\documentclass{article}\begin{document}Experiments.\end{document}"
    proposed = project.check_manuscript_edit({"content": plain + "\n% icml2026.sty"})
    assert not proposed["ok"] and "did not load icml2026.sty" in proposed["diagnostic"]
    assert main.read_text() == stock
    # Preamble commands can legitimately live in an included file.
    (project.latex_dir / "prelude.tex").write_text(r"\usepackage{icml2026}")
    modular = stock.replace(r"\usepackage{icml2026}", r"\input{prelude}")
    assert project.check_manuscript_edit({"content": modular})["ok"]
    # A resumed project can already contain the plain article.
    main.write_text(plain)
    assert project.compile_paper({})["ok"]
    project.config["skip_deep_research"] = False
    monkeypatch.setattr(domain.deep_research, "research_report_status", lambda *_: "verified")
    issues = project.review_snapshot({"preflight": True})["blockers"]
    assert any("recorder" in issue and "icml2026.sty" in issue for issue in issues)
    assert any("No body citation links" in issue for issue in issues)
    assert any("reproducibility appendix" in issue for issue in issues)
    main.write_text(modular.replace(r"\bibliography{references}", ""))
    monkeypatch.setattr(domain.deep_research, "research_report_status", lambda *_: "missing")
    compiled = project.compile_paper({})
    assert compiled["ok"] and "icml2026.sty" in compiled["inputs"]
    assert not any("recorder" in issue for issue in compiled["manuscript_issues"])
    assert "did not load" in project.check_manuscript_edit({"content": plain})["diagnostic"]


def test_manuscript_check_bootstrap_receipt_and_base_digest(tmp_path, monkeypatch):
    project = domain.NativeProject(tmp_path)
    args = {"content": "draft"}
    monkeypatch.setattr(domain, "_manuscript_first_pass", lambda *a: pytest.fail("unexpected pdflatex trial"))
    assert project.invoke("check_manuscript_edit", args)["checked"] is False
    assert list(tmp_path.iterdir()) == []
    assert not project.invoke("check_manuscript_edit", {**args, "base_sha256": "0" * 64})["ok"]

    project.latex_dir.mkdir()
    main = project.latex_dir / "main.tex"
    main.write_text("current draft")
    pdf = project.latex_dir / "main.pdf"
    pdf.write_bytes(b"older PDF")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    project.state.update(state="running", compiled_pdf_sha256=digest, compile={"ok": True, "pdf_sha256": digest})
    assert not project.invoke("check_manuscript_edit", {**args, "base_sha256": "0" * 64})["ok"]
    pdf.write_bytes(b"different file")
    skipped = project.invoke("check_manuscript_edit", args)
    assert skipped["ok"] and not skipped["checked"]
    pdf.write_bytes(b"older PDF")
    project.state["compile"]["ok"] = False
    project.state["state"] = "paused"
    skipped = project.invoke("check_manuscript_edit", args)
    assert skipped["ok"] and not skipped["checked"]


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="requires TeX Live")
def test_manuscript_check_trials_the_actual_edit_without_publishing(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    section = project.latex_dir / "sections/score.tex"
    section.parent.mkdir()
    section.write_text("Score: one.")
    main = project.latex_dir / "main.tex"
    main.write_text(r"\documentclass{article}\begin{document}\input{sections/score}\end{document}")
    assert project.invoke("compile_paper", {})["ok"]
    # An ordinary edit after the receipt keeps the archived PDF authenticated.
    main.write_text(main.read_text().replace(r"\input", r"Results. \input"))
    current = main.read_text()
    base = hashlib.sha256(main.read_bytes()).hexdigest()
    cited = current.replace(r"\input", r"\cite{newsource}\input")
    broken = current.replace(r"\input", r"\undefinedTrialCommand\input")
    (project.latex_dir / "ignored.txt").write_text("must stay outside the trial")

    def files():
        return {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    before = files()
    before_state = json.dumps(project.state, sort_keys=True)
    monkeypatch.setattr(project, "verify_citations", lambda *_: pytest.fail("trial started citation resolution"))
    subprocess_run = domain.subprocess.run
    trials = []

    def record_run(cmd, **kw):
        work = kw["cwd"]
        trials.append((work / "main.tex").read_text())
        assert work != project.latex_dir and kw["env"]["HOME"] == str(work)
        assert kw["timeout"] == 90 and kw["env"]["shell_escape"] == "f" and "-no-shell-escape" in cmd
        assert not (work / "main.pdf").exists() and not (work / "main.log").exists()
        assert not (work / "ignored.txt").exists()
        return subprocess_run(cmd, **kw)

    monkeypatch.setattr(domain.subprocess, "run", record_run)
    assert project.invoke("check_manuscript_edit", {"content": cited, "base_sha256": base}) == {
        "ok": True, "checked": True, "first_pass_preview": {"body_pages": 1, "target": 4},
    }
    assert trials == [cited]
    result = project.invoke("check_manuscript_edit", {"content": broken, "base_sha256": base})
    assert result["ok"] is False and "Undefined control sequence" in result["diagnostic"]
    assert trials == [cited, broken, current]
    assert files() == before and json.dumps(project.state, sort_keys=True) == before_state

    main.write_text(broken)
    incremental = broken.replace("Results.", "Results changed.")
    result = project.invoke("check_manuscript_edit", {"content": incremental})
    assert result["ok"] and not result["checked"] and "also fails" in result["reason"]
    assert trials[-2:] == [incremental, broken]
    assert main.read_text() == broken and project.path.read_bytes() == before[project.path]

    # A single edit can already have fixed the page budget. Count the body in
    # the trial PDF and leave the public build and its receipt untouched.
    main.write_text(current)
    project.config["venue_pages"] = 2
    with_endmatter = (r"\documentclass{article}\begin{document}First page.\newpage Second page."
                      r"\newpage\section*{References}Original text.\end{document}")
    preview = project.invoke("check_manuscript_edit", {"content": with_endmatter})["first_pass_preview"]
    assert (preview["body_pages"], preview["target"]) == (2, 2) and "compile_paper" in preview["next"]
    assert project.latex_dir.joinpath("main.pdf").read_bytes() == before[project.latex_dir / "main.pdf"]
    project.config["venue_pages"] = 4
    assert project.invoke("check_manuscript_edit", {"content": with_endmatter})["first_pass_preview"] == {
        "body_pages": 2, "target": 4,
    }


@pytest.mark.skipif(not shutil.which("pdflatex") or not shutil.which("bibtex"), reason="requires TeX Live")
def test_icml_preview_and_compile_keep_appendix_link_with_the_body(tmp_path, monkeypatch):
    import fitz

    project = open_project(tmp_path, monkeypatch)
    project.config["venue_pages"] = 1
    source = project.latex_dir / "main.tex"
    raw = source.read_text().split(r"\section{Introduction}")[0] + r"""
\section{Conclusion}
Body conclusion.\nocite{*}
\clearpage
\section*{References}
\bibliographystyle{icml2026}
\bibliography{references}
\clearpage
\section*{Reproducibility Appendix}
Archive contents.
\end{document}
"""
    (project.latex_dir / "references.bib").write_text(
        "@book{record, author={A. Author}, title={Technical record}, year={2000}, publisher={Press}}\n")
    source.write_text(raw)
    compiled = project.compile_paper({})
    assert compiled["ok"] and compiled["pages"] == 3
    assert 0 < compiled["body_equivalent_pages"] < 0.75
    with fitz.open(project.latex_dir / "main.pdf") as doc:
        assert "Additional details appear in Appendix" in doc[0].get_text()
        assert "Acknowledgments" in doc[1].get_text() and "Archive contents." in doc[2].get_text()
    canonical = source.read_text()
    # Previously compiled revisions already had a label and an isolated generated link.
    link = "Additional details appear in Appendix"
    old = canonical.replace(link, "\\clearpage\n\n" + link)
    for candidate in (raw, old):
        preview = project.check_manuscript_edit({"content": candidate})["first_pass_preview"]
        assert preview["body_pages"] == preview["target"] == 1
        assert preview["body_equivalent_pages"] == pytest.approx(compiled["body_equivalent_pages"], abs=.03)
        assert "insert_before" in preview["next"]
        assert source.read_text() == canonical
    source.write_text(old)
    assert project.compile_paper({})["pdf_sha256"] == compiled["pdf_sha256"]
    assert source.read_text() == canonical


@pytest.mark.skipif(not shutil.which("pdflatex") or not shutil.which("bibtex"), reason="requires TeX Live")
def test_major_body_cut_cannot_silently_drop_rendered_appendix(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    main = project.latex_dir / "main.tex"
    preamble = main.read_text().split(r"\section{Introduction}")[0]
    short = preamble + r"\section{Results}The two estimates agree."
    body = short + r"\clearpage\section{Methods}Paired estimates.\clearpage\section{Discussion}Sampling error."
    (project.latex_dir / "references.bib").write_text(
        "@book{record, author={A. Author}, title={Technical record}, year={2000}, publisher={Press}}\n")
    refs = r"\nocite{*}\clearpage\section*{References}Source citation.\bibliographystyle{icml2026}\bibliography{references}"
    appendix = r"\clearpage\appendix\section{Reproducibility}Seeds, sample sizes and exact estimators."
    end = r"\end{document}"
    main.write_text(body + refs + appendix + end)
    assert project.compile_paper({})["ok"]
    saved = main.read_bytes()
    proposal = short + refs + end
    checked = project.check_manuscript_edit({"content": proposal})
    assert not checked["ok"] and "removes the appendix present on page" in checked["diagnostic"]
    assert "insert_before" in checked["diagnostic"] and main.read_bytes() == saved
    # An intentional rewrite can keep its appendix in a separate input, regardless of citation keys.
    (project.latex_dir / "supplement.tex").write_text(appendix)
    assert project.check_manuscript_edit({"content": short + refs + r"\input{supplement}" + end})["ok"]
    assert project.check_manuscript_edit({"content": body + refs + end})["ok"]  # no major loss of body
    project.config["venue_pages"] = 1
    assert project.check_manuscript_edit({"content": proposal})["ok"]
    project.config["venue_pages"] = 4
    main.write_text(proposal)
    assert project.check_manuscript_edit({"content": proposal.replace("agree", "agree on all trials")})["ok"]


def test_manuscript_check_handles_missing_tool_and_unsafe_assets(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    pdf = project.latex_dir / "main.pdf"
    pdf.write_bytes(b"prior build")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    project.state.update(compiled_pdf_sha256=digest, compile={"ok": True, "pdf_sha256": digest})
    args = {"content": "draft"}
    monkeypatch.setenv("PATH", str(tmp_path))
    result = project.invoke("check_manuscript_edit", args)
    assert not result["ok"] and not result["checked"] and "pdflatex" in result["diagnostic"]
    (project.latex_dir / "linked").symlink_to(project.state_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="regular files inside paper"):
        project.invoke("check_manuscript_edit", args)


def test_transport_carries_only_requested_result(tmp_path):
    reader = io.StringIO('{"id":"bad","op":"eval","args":{}}\n{"id":"read","op":"inspect_project"}\n')
    writer = io.StringIO()
    domain.serve(domain.NativeProject(tmp_path), reader, writer)
    bad, good = [json.loads(s) for s in writer.getvalue().splitlines()]
    assert not bad["ok"] and bad["id"] == "bad"
    assert good["ok"] and good["result"]["state"] == "new"


def test_original_pdf_keeps_adjacent_columns_out_of_quoted_passages(monkeypatch):
    import io
    from unittest.mock import Mock
    import fitz
    from ark import literature_evidence as evidence

    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_textbox((60, 60, 270, 160), "The original authors measured\na paired improvement.", fontsize=11)
        page.insert_textbox((330, 60, 530, 160), "0.2  0.4  0.6\nFigure axes", fontsize=11)
        pdf = io.BytesIO(doc.tobytes())
    pdf.headers = {}
    monkeypatch.setattr(evidence, "build_opener", lambda *args: Mock(open=Mock(return_value=pdf)))
    page, = evidence._paper_excerpt("https://arxiv.org/pdf/example")
    assert "The original authors measured a paired improvement." in " ".join(page.split())
    assert "Figure axes" in page


def test_survey_doi_opens_the_identified_paper_and_checks_its_printed_title(monkeypatch):
    evidence = domain.literature_evidence
    title = "Comparing Rank Queries"
    metadata = {"title":title, "externalIds":{"DOI":"10.1234/query","ACL":"2024.acl-short.11"}}
    monkeypatch.setattr(evidence.citation, "_http_get_json", lambda *a: metadata)
    pdf = ["Comparing Rank\nQueries", "The authors measured paired rank errors. " * 10]
    monkeypatch.setattr(evidence, "_paper_excerpt", lambda url: pdf)
    result = evidence.repository_passage("10.1234/query", title)
    assert result["url"] == "https://aclanthology.org/2024.acl-short.11.pdf"
    assert "[Page 2]" in result["passage"]
    pdf[0] = "Another paper"
    assert evidence.repository_passage("10.1234/query", title) is None
    pdf[0] = title
    assert evidence.repository_passage("10.1234/different", title) is None
    assert evidence.repository_passage("10.48550/arxiv.2204.01469", title)["url"] == "https://arxiv.org/pdf/2204.01469"
    assert evidence.repository_passage("10.18653/v1/n19-1001", title)["url"] == "https://aclanthology.org/N19-1001.pdf"


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


def test_survey_leads_retain_report_locations_without_treating_redirects_or_code_as_papers():
    report = '\n'.join([
        'A paper titled "Which populations benefit from calibration" (DOI: 10.1234/A(B)).',
        '[paper](https://doi.org/10.1234/a(b)) [1](https://vertexaisearch.cloud.google.com/10.9999/bogus)',
        'The preprint is at https://arxiv.org/abs/2101.01234v2 and arXiv:2101.01234.',
        '```python', 'doi="10.1111/example"', '```',
        'Their paper was titled “Proper scoring for shifted populations”.',
        'The paper "Early verification of forecasts" used DOI: 10.1234/078<OO>2.0.',
    ])
    assert domain.literature_evidence.source_leads(report) == [
        {"query": "10.1234/A(B)", "report_line": 1, "reported_title": "Which populations benefit from calibration"},
        {"query": "10.48550/arXiv.2101.01234", "report_line": 3},
        {"query": "Proper scoring for shifted populations", "report_line": 7},
        {"query": "Early verification of forecasts", "report_line": 8},
    ]


def test_survey_candidates_use_official_abstract_and_cache_only_confirmed_dois(monkeypatch):
    evidence = domain.literature_evidence
    calls = []
    def fetch(url, headers, timeout):
        calls.append(url)
        if url.endswith("missing"):
            return json.dumps({"DOI": "10.1234/other", "title": "Wrong paper", "abstract": "Wrong abstract"})
        return json.dumps({"DOI": "10.1234/METHOD", "title": "Canonical title",
                           "abstract": "<p>Original &amp; measured.</p>"})
    monkeypatch.setattr(evidence.citation, "_http_get", fetch)
    report = 'See doi:10.1234/method.\nThe paper "A mistaken report title" has DOI:10.1234/method.\nSee doi:10.1234/missing.'
    leads = evidence.source_leads(report)
    cache = {}
    resolved = evidence.survey_originals(leads, cache)
    assert len(calls) == 2 and set(cache) == {"10.1234/method"}
    assert resolved[0]["reported_title"] == "A mistaken report title"
    assert resolved[0]["official_title"] == "Canonical title"
    assert resolved[0]["abstract_excerpt"] == "Original & measured."
    assert "official_title" not in resolved[1]
    evidence.survey_originals(leads, cache)
    assert len(calls) == 3 and calls[-1].endswith("missing")


def test_survey_title_registration_uses_the_matched_deposit_on_old_checkpoints(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    (project.state_dir / "deep_research.md").write_text('The paper "Exactly Selected Title" has DOI: 10.1234/chosen.')
    monkeypatch.setattr(domain.deep_research, "research_report_status", lambda state: "verified")
    meta = {"url":"https://doi.org/10.1234/chosen", "source":"Deposited author abstract (DOI registry)",
            "title":"Exactly Selected Title", "passage":"Measured the paired trials."}
    monkeypatch.setattr(domain.literature_evidence, "doi_original", lambda doi: meta)
    monkeypatch.setattr(domain.citation, "search_papers", lambda *args, **kwargs: pytest.fail("ambiguous title search"))
    monkeypatch.setattr(domain.citation, "_fetch_bibtex_from_doi", lambda doi:
                        "@article{trial, title={Exactly Selected Title}, author={Lee, B}, doi={10.1234/chosen}, year={2025}}")
    result = project.lookup_papers({"query": "Exactly Selected Title", "add_to_bibliography": True})
    assert result["saved"] and result["cite_key"] == "trial" and result["passage"] == meta["passage"]
    assert "bibtex_key" not in project.state["survey_originals"]["10.1234/chosen"]


def test_strict_research_manuscript_preserves_live_bibliography(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    project.config["skip_deep_research"] = False
    source = project.latex_dir / "main.tex"
    previous = source.read_text()
    manual = r"\section*{References}" + "\n[1] An unlinked work."
    proposed = previous.replace(r"\bibliography{references}", manual)
    assert proposed != previous
    result = project.check_manuscript_edit({"content": proposed})
    assert not result["ok"] and "lookup_papers" in result["diagnostic"]
    assert project.check_manuscript_edit({"content": previous + "\n% " + manual})["ok"]
    assert project.check_manuscript_edit({"content": proposed + r"\input{refs}"})["ok"]
    manual = r"\begin{thebibliography}{9}\bibitem{madeup} A typed reference.\end{thebibliography}"
    assert "lookup_papers" in project.check_manuscript_edit({"content": manual})["diagnostic"]
    source.write_text(proposed)
    assert project.check_manuscript_edit({"content": proposed + "\n"})["ok"]


def test_register_selected_publication_keeps_existing_cite_keys(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    paper = domain.citation.Paper(title="Measured search costs", authors=["A. Author"], year=2020, doi="10.1234/search")
    official = "@article{author2020,\n title={Measured search costs},\n doi={10.1234/search},\n year={2020}\n}"
    monkeypatch.setattr(domain.citation, "search_papers", lambda *a, **kw: [paper])
    monkeypatch.setattr(domain.citation, "fetch_bibtex", lambda p: official)
    monkeypatch.setattr(domain.citation, "_fetch_bibtex_from_doi", lambda doi: official)
    primary = tmp_path / "sources/primary.txt"
    primary.parent.mkdir(exist_ok=True)
    primary.write_text(json.dumps({"source":"Publisher abstract", "passage":"The two methods used equal inputs."}))
    monkeypatch.setattr(project, "_source_record", lambda entry: "sources/primary.txt")
    bib = project.latex_dir / "references.bib"
    bib.write_text("% my references\n")
    assert not project.lookup_papers({"query": "search", "add_to_bibliography": True})["saved"]
    assert "saved" not in project.lookup_papers({"query": "Measured search costs"})
    assert bib.read_text() == "% my references\n"
    request = {"query": "Measured search costs", "add_to_bibliography": True}
    registered = project.lookup_papers(request)
    assert registered["cite"] == r"\cite{author2020}" and registered["passage"] == "The two methods used equal inputs."
    with monkeypatch.context() as direct:
        direct.setattr(domain.citation, "search_papers", lambda *a, **kw: [])
        assert project.lookup_papers(dict(request, query=paper.doi))["cite_key"] == "author2020"
        direct.setattr(domain.citation, "_fetch_bibtex_from_doi", lambda doi: official.replace("/search", "/other"))
        assert not project.lookup_papers(dict(request, query=paper.doi))["saved"]
    assert bib.read_text().count("@article") == 1
    bib.write_text(official.replace("author2020", "existing"))
    assert project.lookup_papers(request)["cite_key"] == "existing"
    collision = official.replace("10.1234/search", "10.1234/other").replace("Measured search costs", "Another work")
    bib.write_text(collision)
    assert not project.lookup_papers(request)["saved"] and bib.read_text() == collision
    official = official.replace("year={2020}", "author={A. Author}, year={2020}")
    dubious = official.replace("author2020", "attributed").replace("A. Author", "Wrong Person")
    bib.write_text(dubious)
    assert not project.lookup_papers(request)["saved"] and bib.read_text() == dubious
    bib.write_text(dubious.replace("doi={10.1234/search}", 'journal={Conference}'))
    reply = project.lookup_papers(request)
    assert reply["cite_key"] == "author2020" and reply["check_existing_keys"] == ["attributed"]


def test_body_citations_trace_primary_records_and_survey_independently(tmp_path, monkeypatch):
    import fitz
    from contextlib import nullcontext

    project = open_project(tmp_path, monkeypatch)
    bib = project.latex_dir / "references.bib"
    bib.write_text("\n".join(f"@article{{{k}, title={{{k} short title}}, author={{A Researcher}}, year={{2025}}}}"
                             for k in ("main", "extra", "supplement")))
    project.config["skip_deep_research"] = False
    report = project.state_dir / "deep_research.md"
    report.write_text("# Survey\n\n## References\nA relevant study (doi:10.1234/method).\n")
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    (project.state_dir / "deep_research_receipt.json").write_text(json.dumps({
        "provider": "google", "status": "completed", "interaction_id": "fixture", "report_sha256": sha(report)
    }))
    checked = {}
    for key, source_url in (("supplement", "https://doi.org/10.1234/appendix"),
                            ("main", "https://doi.org/10.1234/method"), ("extra", "https://doi.org/10.1234/method2")):
        relative = "sources/" + hashlib.sha256(source_url.encode()).hexdigest()[:24] + ".txt"
        source = tmp_path / relative
        source.parent.mkdir(exist_ok=True)
        # A DOI in the quoted passage can identify a different paper.
        source.write_text(f"Indexed abstract\nA short title\n{source_url}\n\nIt used https://doi.org/10.1234/method\n")
        checked[key] = {"status": "SINGLE_SOURCE", "source": relative, "source_sha256": sha(source)}
    (project.state_dir / "native_citations.json").write_text(json.dumps({"sha256": sha(bib), "checked": checked}))

    def page(index, items):
        links = [{"nameddest": "cite." + key, "page": 1, "from": fitz.Rect(x, y, x+30, y+10)}
                 for key, x, y in items]
        return SimpleNamespace(number=index, rect=fitz.Rect(0, 0, 600, 800), get_links=lambda: links)
    pages = [page(0, [("extra", 320, 80)]), page(1, [("0@main", 50, 80), ("supplement", 350, 300)]),
             page(2, [("supplement", 40, 120)])]
    monkeypatch.setattr(fitz, "open", lambda *a: nullcontext(pages))
    monkeypatch.setattr(domain.literature_evidence, "body_page_boundary", lambda doc: (1, (200, 0)))
    trace = project._research_citations()
    assert (trace["matched"], trace["total"], trace["references_line"]) == (1, 2, 3)
    assert trace["verified_works"] == 1
    assert trace["body_citations"] == {
        "main": {"source": checked["main"]["source"], "pages": [2], "report_line": 4},
        "extra": {"source": checked["extra"]["source"], "pages": [1]},
    }
    assets = {name: {"kind": "primary_source"} for name in ("sources/archived.txt", *(r["source"] for r in checked.values()))}
    assert project._opening_review_evidence({"artifacts": assets}, trace) == [
        checked["main"]["source"], checked["extra"]["source"],
    ]
    (tmp_path / checked["main"]["source"]).write_text("different source material")
    assert project._research_citations()["body_citations"]["main"] == {"pages": [2]}
    project.config["layout_mode"] = "flexible"
    assert project._research_citations() is None
    project.config["layout_mode"] = "strict"
    report.write_text("replaced after verification")
    assert project._research_citations() is None


def test_review_reopens_current_page_text_at_unresolved_locations(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    pdf = "paper/main.pdf"
    snapshot = {"revision": "new", "artifacts": {}, "conclusion": {"artifact": pdf, "page": 4, "start": 220}}
    def passage(page):
        return {"artifact": pdf, "quote": "Last revision's disputed text", "page": page}
    previous = {"revision": "old", "issues": [
        {"status": "resolved", "evidence": [passage(3)]},
        {"status": "new", "evidence": [passage(2), passage(2), passage(False), passage(8)]},
        {"status": "still_present", "evidence": [passage(1), passage(4)]},
    ]}
    requested = project._opening_review_evidence(snapshot, previous=previous, pages=4)
    assert requested == [snapshot["conclusion"], {"artifact": pdf, "page": 2}, {"artifact": pdf, "page": 1}]
    assert project._opening_review_evidence(snapshot, previous=previous, pages=0) == [snapshot["conclusion"]]
    previous["revision"] = "new"
    assert project._opening_review_evidence(snapshot, previous=previous, pages=4) == [snapshot["conclusion"]]


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
    journal = (project.state_dir / "research_events.jsonl").read_text().splitlines()
    assert sum(json.loads(line)["action"] == "review_started" for line in journal) == 1
    with pytest.raises(ValueError, match="budget exhausted"):
        project.review_snapshot({"session_id": "child2"})


def test_reviewer_commits_scope_before_opening_paper_or_prior_findings(tmp_path, monkeypatch):
    import fitz

    project = open_project(tmp_path, monkeypatch)
    pdf = project.latex_dir / "main.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((40, 40), "Conclusion")
        page.insert_text((40, 80), "The loss fell because the treatment improved uptake.")
        doc.save(pdf)
    (project.latex_dir / "main.tex").write_text("Author's explanation for the loss.")
    results = tmp_path / "results/loss.json"
    results.write_text('[{"trial":1,"treated":12.345,"control":18.765}]')
    run = {"status":"completed", "artifact":"results/loss.json", "sha256":domain._sha(results)}
    script = tmp_path / "scripts" / "method.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text("def measure(x): return sum(x)")
    run.update(backend="slurm", script="scripts/method.py", code_sha256=domain._sha(script))
    snap = domain.review.build_snapshot(tmp_path, required=["experiment"], expected_measurement=run["artifact"],
                                        measurement_run=run)
    project.state["compiled_pdf_sha256"] = domain._sha(pdf)
    monkeypatch.setattr(project, "_check_compiled", lambda: None)
    monkeypatch.setattr(project, "_snapshot", lambda: snap)
    packet = project.review_snapshot({"session_id":"child1"})
    assert packet["inference_test"] is True
    assert "treated" in packet["scope_brief"]
    for hidden in ("12.345", "improved uptake", "Author's explanation"):
        assert hidden not in packet["scope_brief"]
    read = {"revision":snap["revision"], "artifact":"paper/main.pdf", "page":1}
    with pytest.raises(ValueError, match="Commit"):
        project.invoke("review_evidence", read)
    note = {"comparison":"Paired loss at fixed trial.", "alternative":"Exposure may differ.",
            "discriminator":"Directly measure exposure in each arm."}
    assert project.invoke("commit_evidence_scope", {**note, "revision":snap["revision"]}) == note
    signed = {"revision":snap["revision"], "version":domain.review.REVIEW_VERSION, "evidence_scope":note}
    assert domain.review.record_is_current(snap, signed)
    assert not domain.review.record_is_current(snap, {**signed, "evidence_scope":{}})
    assert "improved uptake" in project.invoke("review_evidence", read)["text"]
    calls = []
    def calculate(runtime, program, ids, columns):
        calls.append(ids["case"])
        return {"premise":6, "outcome":4 if ids["case"] == "A" else 5}
    monkeypatch.setattr(domain.review_control, "reproduce", calculate)
    args = dict(revision=snap["revision"], passage={"selector":"conclusion"}, premise="sum", outcome="maximum",
                assumptions="Two positive integers.", program="def reproduce(ids): return {}")
    with pytest.raises(ValueError, match="current empirical review"):
        project.invoke("test_inference", {**args, "revision":"stale"})
    receipt = project.invoke("test_inference", args)["receipt"]
    assert receipt["same_premise"] and receipt["distinguishable_outcome"]
    assert receipt["passage"]["page"] == 1 and receipt["cases"]["B"]["outcome"] == 5
    assert project.invoke("test_inference", args)["receipt"] == receipt
    assert calls == ["A", "B"] and project.control_attempts == 1
    project.review_snapshot({"session_id":"child1"})
    with pytest.raises(ValueError, match="already committed"):
        project.invoke("commit_evidence_scope", {**note, "comparison":"Updated after reading.", "revision":snap["revision"]})
    project.review_snapshot({"session_id":"child2"})
    with pytest.raises(ValueError, match="Commit"):
        project.invoke("review_evidence", read)
    assert "evidence_scope" not in domain.review.build_snapshot(tmp_path, expected_measurement=run["artifact"])


def test_review_preview_leaves_verdict_unpublished_and_rechecks_current_evidence(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    main = project.latex_dir / "main.tex"
    main.write_text("The study examines a paired control.")
    (project.latex_dir / "main.pdf").write_bytes(b"frozen manuscript")
    snapshot = project._snapshot()
    project.snapshot = snapshot
    args = {"revision": snapshot["revision"], "session_id": "child1", "observed": [], "verdict": {}}
    first = project.invoke("check_review", args)
    assert first["valid"] is False and "revision" in first["diagnostic"]
    checks = {d: {"status": "unassessed", "reason": "Unavailable.", "evidence": []}
              for d in ("manuscript", "experiment", "citations", "visual")}
    checks["manuscript"] = {"status": "assessed", "reason": "Read the manuscript.",
                            "evidence": [{"artifact": "paper/main.tex", "quote": main.read_text()}]}
    args["verdict"] = {"revision": snapshot["revision"], "summary": "The study examines controls.",
                       "alternative": "Results have not been independently measured.",
                       "strengths": ["The question is explicit."], "checks": checks, "issues": [],
                       "verdict": "unassessed"}
    assert project.invoke("check_review", args) == {"valid": True}
    assert not (project.state_dir / "native_review.json").exists()
    with pytest.raises(ValueError, match="Unknown reviewer evidence"):
        project.invoke("check_review", {**args, "observed": [{"artifact": "paper/main.tex"}]})
    main.write_text("The owner changed this manuscript.")
    with pytest.raises(ValueError, match="changed while the review"):
        project.invoke("record_review", args)
    assert not (project.state_dir / "native_review.json").exists()


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
    assert main.read_text().count(r"\ifdim\wd\iipMeasuredBox>\columnwidth") == 1
    restored = main.read_text()
    include = r"\input{" + r["path"] + "}"
    main.write_text(restored.replace(include, ""))
    project._restore_measured_table()
    assert main.read_text().count(include) == main.read_text().count(r"\label{tab:i2p-measurements}") == 1
    authored = restored.replace(include, r"\begin{tabular}{rr}1&2\end{tabular}")
    main.write_text(authored)
    project._restore_measured_table()
    assert main.read_text() == authored
    legacy = "\n".join([r"\begin{table*}[t]", r"\centering", r"\small",
                        r"\caption{Recorded experimental measurements.}\label{tab:i2p-measurements}",
                        include, r"\end{table*}", r"\end{document}"])
    for authored in (legacy.replace("[t]", "[b]"), legacy.replace("Recorded experimental", "Additional")):
        main.write_text(authored)
        project._restore_measured_table()
        assert main.read_text() == authored
    main.write_text(legacy)
    project._restore_measured_table()
    migrated = main.read_text()
    assert migrated.count(include) == 1 and r"\iipMeasuredTable{table*}{t}" in migrated
    assert r"\iipMeasuredTable{table}{!htbp}" in migrated
    project._restore_measured_table()
    assert main.read_text() == migrated
    main.write_text(legacy.replace(include + "\n", ""))
    project._restore_measured_table()
    assert main.read_text() == migrated
    results.write_text('{"pairs":[]}')
    with pytest.raises(ValueError, match="worker receipt"):
        project.invoke("render_results_table", args)


def test_measured_headers_support_mixed_math_and_keep_other_tex_literal():
    from ark.measurement_table import render

    def heading(label):
        return render([{"value": 7}], "", [{"path": "value", "label": label}]).splitlines()[2]

    assert heading(r"$\bar{K}_{\mathrm{obs}}$") == r"\(\bar{K}_{\mathrm{obs}}\) \\"
    assert heading(r"$\Delta$MAE / $\sigma^2$ (\%)") == r"\(\Delta\)MAE / \(\sigma^2\) (\%) \\"
    assert heading(r"$n$") == r"\(n\) \\"
    assert heading("A & B_1 (95%)") == r"A \& B\_1 (95\%) \\"
    assert heading("Cost $5 to $10") == r"Cost \$5 to \$10 \\"
    for label in (r"$\custom{A}$", r"$\bar{K$", "$n^^41$", "$a_1_2$", "$n^$", "$x&y$"):
        assert heading(label).startswith(r"\$") and r"\(" not in heading(label)
    # A worker-supplied string is data and must never opt into heading syntax.
    assert r"\$\textbackslash{}Delta\$" in render([{"value": r"$\Delta$"}], "", [{"path": "value"}])


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="requires TeX Live")
def test_measured_float_uses_actual_column_width(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    source = project.latex_dir / "main.tex"
    source.write_text(r"""\documentclass[twocolumn]{article}
\AddToHook{env/table/begin}{\typeout{MEASURED-FLOAT-SINGLE}}
\AddToHook{env/table*/begin}{\typeout{MEASURED-FLOAT-WIDE}}
\begin{document}\section{Results}Measured loss.\end{document}""")
    rows = tmp_path / "results/pairs.json"
    rows.write_text('[{"loss": 0.5}]')
    project.state["experiment"] = {"status": "completed", "artifact": "results/pairs.json",
                                   "sha256": hashlib.sha256(rows.read_bytes()).hexdigest()}
    for label, expected in [("Loss", "SINGLE"), ("W" * 32, "WIDE")]:
        project.render_results_table({"name": "loss", "columns": [{"path": "loss", "label": label}]})
        result = project.invoke("compile_paper", {})
        assert result["ok"] and "tables/loss.tex" in result["inputs"]
        log = (project.latex_dir / "main.log").read_text()
        assert [line for line in log.splitlines() if line.startswith("MEASURED-FLOAT-")] == [
            "MEASURED-FLOAT-" + expected
        ]


def test_upgrade_measured_headers_requires_exact_old_projection_and_frozen_rows(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    data = [{"loss": 0.125}]
    results = tmp_path / "results/metrics.json"
    results.write_text(json.dumps(data))
    digest = lambda b: hashlib.sha256(b).hexdigest()
    project.state["experiment"] = {"status":"completed", "artifact":"results/metrics.json",
                                   "sha256":digest(results.read_bytes())}
    columns = [{"path":"loss", "label":r"$\Delta$ loss"}]
    project.render_results_table({"name":"metrics", "columns":columns})
    record = project.state["tables"]["metrics"]
    table = project.latex_dir / record["path"]
    (project.latex_dir / "main.tex").write_text(r"\input{tables/metrics}")
    fresh = table.read_bytes()
    old = fresh.replace(r"\(\Delta\)".encode(), r"\$\textbackslash{}Delta\$".encode())
    assert old != fresh
    record["sha256"] = digest(old)
    table.write_bytes(old + b"% custom")
    project._restore_measured_table()
    assert table.read_bytes() == old + b"% custom" and record["sha256"] == digest(old)
    table.write_bytes(old)
    original_data = results.read_bytes()
    results.write_text('[{"loss":0.25}]')
    project._restore_measured_table()
    assert table.read_bytes() == old and record["sha256"] == digest(old)
    results.write_bytes(original_data)
    columns[0]["digits"] = 4
    project._restore_measured_table()
    assert table.read_bytes() == old and record["sha256"] == digest(old)
    del columns[0]["digits"]
    for starting in (old, fresh):  # old projection, then interrupted migration before the receipt write
        table.write_bytes(starting)
        record["sha256"] = digest(old)
        project._restore_measured_table()
        assert table.read_bytes() == fresh and record["sha256"] == digest(fresh)
        assert json.loads(project.path.read_text())["tables"]["metrics"] == record


def test_strict_table_keeps_worker_conditions_across_render_and_resume(tmp_path, monkeypatch):
    from ark import measurement_table

    project = open_project(tmp_path, monkeypatch)
    project.config["layout_mode"] = "strict"
    rows = [{"seed": s, "condition": c, "loss": (i + 1) / 9}
            for i, (s, c) in enumerate([(2, "A"), (2, "B"), (1, "A"), (1, "B")])]
    measurements = tmp_path / "results/pairs.json"
    measurements.write_text(json.dumps(rows))
    project.state["experiment"] = {"status": "completed", "artifact": "results/pairs.json",
                                   "sha256": hashlib.sha256(measurements.read_bytes()).hexdigest()}
    columns = [{"path": "condition"}, {"path": "loss"}]
    args = {"name": "errors", "columns": columns}
    record = project.render_results_table(args)
    assert [c["path"] for c in record["columns"]] == ["seed", "condition", "loss"]
    assert "2 & B & 0.22" in record["preview"] and "1 & A & 0.33" in record["preview"]
    # A pre-upgrade table can have an intact source and measurement receipt while
    # hiding which observation each number belongs to.
    project.state["tables"]["errors"]["columns"] = columns
    project.state["compile"] = {"inputs": [record["path"]]}
    assert any("omit their measured identifiers (seed)" in msg for msg in project._strict_paper_issues())
    project.render_results_table(args)
    assert not any("omit their measured identifiers" in msg for msg in project._strict_paper_issues())
    assert measurement_table.with_row_identifiers([{"value": 2}, {"value": 3}], "", columns) == columns
    assert measurement_table.with_row_identifiers([{"seed": 7}, {"seed": 7}], "", columns) == columns
    project.config["layout_mode"] = "flexible"
    assert project.render_results_table(args)["columns"] == columns


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="requires TeX Live")
def test_strict_compiled_table_cannot_hide_a_conflicting_transcription(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    rows = [{"fold": 1, "condition": "A", "time": 2.4689, "rate": 0.08761},
            {"fold": 2, "condition": "A", "time": 3.14159, "rate": 0.12349},
            {"fold": 1, "condition": "B", "time": 1.2349, "rate": 0.2345}]
    measured = tmp_path / "results/pairs.json"
    measured.write_text(json.dumps({"observations": rows}))
    project.state["experiment"] = {"status": "completed", "artifact": "results/pairs.json",
                                   "sha256": domain._sha(measured)}
    args = {"name": "outcomes", "rows_pointer": "/observations", "columns": [
        {"path": "time", "label": "Time"}, {"path": "rate", "label": "Rate %", "percent": True}]}
    table = project.render_results_table(args)
    source = project.latex_dir / "main.tex"
    source.write_text(r"""\documentclass{article}
\begin{document}\section{Results}
\input{observations}
\appendix\section{Measured outcomes}
\input{tables/outcomes.tex}
\end{document}""")
    manuscript = project.latex_dir / "observations.tex"
    # Reordered columns, a subset of the rows in reverse order, different
    # precision from the generated input, and a composite row identifier.
    body = r"""\begin{tabular}{lrrr}
Condition & Rate \% & Time & Fold \\
B & 23.5 & 1.235 & 1 \\
A & 12.3 & 3.124 & 2 \\
\end{tabular}"""
    hidden = body.replace("3.124", "9.001")
    other = body.replace("Rate", "Rank")
    independent = body.replace("23.5", "88.1").replace("12.3", "89.2").replace("3.124", "3.142")
    summary = body.replace("Fold", "Folds averaged")
    manuscript.write_text("\n\n".join([body, r"\iffalse" + hidden + r"\fi", other, independent, summary]))

    result = project.compile_paper({})
    assert result["ok"] and table["path"] in result["inputs"]
    issue, = [i for i in result["manuscript_issues"] if "disagrees with the worker measurements" in i]
    assert "observations.tex:1 (PDF p. 1)" in issue and "Condition=A, Fold=2, Time prints 3.124 (worker 3.142)" in issue
    assert issue in project.review_snapshot({"preflight": True})["blockers"]
    assert "Rate %" not in issue and "1.235" not in issue
    manuscript.write_text(manuscript.read_text().replace(body, body.replace("3.124", "3.142"), 1))
    assert not any("disagrees with the worker measurements" in i for i in project.compile_paper({})["manuscript_issues"])


def test_render_plot_discovery_and_strict_delivery_require_actual_read(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    registered = plan()
    project.state["plan"] = registered
    results = tmp_path / registered["expected"]
    results.write_text('[{"arm": {"loss": 0.738421}, "n": 2}]')
    sha = hashlib.sha256(results.read_bytes()).hexdigest()
    project.state["experiment"] = {"status": "completed", "artifact": registered["expected"], "sha256": sha}
    assert project.invoke("render_plot", {})["available"] == [{"rows_pointer": "", "rows": 1, "columns": ["arm.loss", "n"]}]
    args = {"filename": "loss.png", "kind": "points", "x": {"path": "n"}, "series": [{"path": "arm.loss"}]}
    with pytest.raises(ValueError, match="Missing measured column"):
        project.invoke("render_plot", {**args, "series": [{"path": "arm.other"}]})

    image = project.figures_dir / args["filename"]
    image.write_bytes(b"published fixture")
    record = {"status": "completed", "backend": "local", "job_id": "123", "artifact": "paper/figures/loss.png",
              "sha256": hashlib.sha256(image.read_bytes()).hexdigest(), "input_sha256": sha}
    monkeypatch.setattr(domain.native_experiment, "execute", lambda *a, **kw: dict(record))
    legacy = project.invoke("render_plot", {"script": "scripts/old.py", "filename": "loss.png"})
    assert "no measurement-read evidence" in legacy["measurement_note"]
    project.state["compile"] = {"inputs": ["figures/loss.png"]}
    assert any("read /study/results.json" in i for i in project._strict_paper_issues())
    record["input_access"] = "worker_observed_read"
    assert project.invoke("render_plot", args)["input_access"] == "worker_observed_read"
    assert project._strict_paper_issues() == []
    published = dict(project.state["plots"]["loss.png"])
    unknown = {"identity": "i2p-unresolved", "backend": "slurm", "status": "submission_unknown",
               "error": "Submission response was unavailable."}
    monkeypatch.setattr(domain.native_experiment, "execute", lambda *a, **kw: dict(unknown))
    assert project.invoke("render_plot", {**args, "y_label": "Updated label"}) == unknown
    assert project.state["plot_attempts"]["loss.png"] == unknown
    assert project.state["plots"]["loss.png"] == published
    assert project._strict_paper_issues() == []
    journal = (project.state_dir / "research_events.jsonl").read_text()
    assert "slurm submission i2p-unresolved: submission_unknown" in journal
    project.state["compile"]["inputs"] = []
    assert any("Include a scientific plot" in i for i in project._strict_paper_issues())


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
    with pytest.raises(RuntimeError, match="Google key"):
        project.invoke("generate_concept_figure", {**args, "purpose": "compare_conditions"})
    image.write_bytes(b"unverified replacement")
    with pytest.raises(RuntimeError,match="Google key"):
        project.invoke("generate_concept_figure",args)


@pytest.mark.parametrize("verdict", ["passed", "contradiction"])
def test_native_comparisons_generate_and_inspect_one_grounded_study_panel(tmp_path, monkeypatch, verdict):
    from PIL import Image
    from ark import nano_banana

    project = open_project(tmp_path, monkeypatch)
    approved = project.invoke("plan_experiment", {
        **plan(), "question": "How do irrelevant features affect nearest-neighbor regression?",
        "baseline": "KNeighborsRegressor on 10 features (5 signal and 5 noise)",
        "comparison": "KNeighborsRegressor on 5 signal features",
        "metric": "RMSE and MAE",
    })
    artifact = tmp_path / approved["expected"]
    artifact.write_text('{"summary": {"paired_probes": 8}}')
    project.state["experiment"] = {
        "status": "completed", "artifact": approved["expected"], "sha256": domain._sha(artifact),
        "plan_sha256": hashlib.sha256(json.dumps(approved, sort_keys=True).encode()).hexdigest(),
    }
    project.save()
    (project.state_dir / "experiment_plan.yaml").write_text("unsupported class clusters")
    args = {
        "name": "method",
        "caption": "Left: noise obscures a signal manifold. Right: the neighborhood regains precision.",
        "description": "Show class clusters and a shrinking neighborhood in two panels.",
    }
    if verdict == "passed":
        # A layout preference cannot turn a requested comparison into generated scientific labels.
        args.update(purpose="compare_conditions", representation="schematic")
    mechanism = "Show how Euclidean distances select the nearest training observations for one query."
    monkeypatch.setattr(domain.deep_research, "get_gemini_api_key", lambda: "fixture")
    monkeypatch.setattr(nano_banana, "_call_gemini_text", lambda *a: pytest.fail("invented new comparison arms"))
    monkeypatch.setattr(nano_banana, "repair_concept_image", lambda *a: pytest.fail("redrew a typeset image"))
    draws, reviews = [], []

    def draw(prompt, path, key, model, *, aspect_ratio, receipt):
        draws.append(prompt)
        assert path.suffix == ".image" and aspect_ratio == "1:1" and approved["question"] in prompt
        assert "No words, numerals" in prompt and mechanism not in prompt
        assert approved["baseline"] not in prompt and "class clusters" not in prompt and "paired_probes" not in prompt
        buf = io.BytesIO()
        Image.new("RGB", (50, 40), "silver").save(buf, "PNG")
        nano_banana._save_generated_image(buf.getvalue(), path, receipt, provider="google", image_model=model)
        return True

    def inspect(path, design, provider, key, model):
        reviews.append(domain._sha(path))
        checks = {name: {"status": "consistent", "observed": "Approved condition."}
                  for name in nano_banana._SEMANTIC_CHECKS}
        checks["result_marks"]["depiction"] = "method"
        checks["printed_labels"]["transcription"] = ["Baseline", "Comparison"]
        if "subject-only illustration" in design:
            assert approved["baseline"] in design and approved["comparison"] in design
            assert "class clusters" not in design and "regains precision" not in design
            if verdict == "contradiction":
                checks["comparison_groups"].update(status="contradiction", expected="Only two conditions.",
                                                   correction="Remove the unsupported third condition.")
        else:
            assert "must be wordless and data-free" in design and mechanism in design
            assert approved["comparison"] in design and "paired_probes" not in design
            checks["printed_labels"]["transcription"] = ["Shared protocol", "Operations compared", "Readout"]
        return {"status": nano_banana.semantic_verdict(checks), "model": model, "provider": provider, "checks": checks}

    monkeypatch.setattr(nano_banana, "generate_figure", draw)
    monkeypatch.setattr(nano_banana, "review_concept_image", inspect)
    result = project.invoke("generate_concept_figure", args)
    path = tmp_path / result["path"]
    get_entry = lambda: domain.figure_manifest.load_manifest(project.figures_dir)["figures"][path.name]
    entry = get_entry()
    assert len(draws) == len(reviews) == 1
    assert result["representation"] == "study_panel" and result["semantic_pass"] == (verdict == "passed")
    panel = entry["panel_plan"]
    assert [(g["label"], g["description"], g["evidence"]) for g in panel["groups"]] == [
        ("Baseline", approved["baseline"], approved["baseline"]),
        ("Comparison", approved["comparison"], approved["comparison"]),
    ]
    assert all(approved[k] in panel["caption"] for k in ("baseline", "comparison", "metric"))
    assert result.get("caption") == (panel["caption"] if verdict == "passed" else None)
    assert domain._sha(path) == result["sha256"] == entry["artifact_sha256"]
    assert domain._sha(path.parent / entry["illustration_file"]) == entry["payload_sha256"]
    assert entry["semantic_review"]["label_verification"]["coverage"] == "reproduced_typeset"
    assert project.invoke("generate_concept_figure", args)["semantic_pass"] == (verdict == "passed")
    assert len(draws) == len(reviews) == 1

    request = {"name": "method", "caption": "How neighbors are selected for one query.", "description": mechanism}
    schematic = project.invoke("generate_concept_figure", {**request, "purpose": "explain_mechanism"})
    assert schematic["representation"] == "schematic" and schematic["semantic_pass"]
    mechanism_entry = get_entry()
    assert mechanism_entry["representation"] == "subject_with_mechanism_panel"
    assert mechanism_entry["payload_sha256"] == domain._sha(path.parent / mechanism_entry["illustration_file"])
    assert mechanism_entry["payload_sha256"] != mechanism_entry["artifact_sha256"] == reviews[-1]
    assert mechanism_entry["semantic_review"]["label_verification"]["coverage"] == "reproduced_typeset"
    assert schematic["caption"] == mechanism_entry["panel_plan"]["caption"]
    assert get_entry()["native_description"] != entry["native_description"]
    assert project.invoke("generate_concept_figure", {**request, "representation": "schematic"})["cached"]
    assert len(draws) == len(reviews) == 2
    with pytest.raises(ValueError, match="mechanism_steps"):
        project.invoke("generate_concept_figure", {**request, "purpose": "explain_mechanism",
                                                  "mechanism_steps": ["controls[0]", "unverified number"]})
    assert len(draws) == len(reviews) == 2
    artifact.write_text('{"summary": "tampered"}')
    assert "tampered" not in nano_banana.native_study_design(project.state, project.state_dir)


def test_unavailable_initial_panel_does_not_trigger_a_second_image(tmp_path, monkeypatch):
    from ark import nano_banana

    project = open_project(tmp_path, monkeypatch)
    project.invoke("plan_experiment", plan())
    args = {"name": "method", "caption": "A comparison", "description": "Two conditions"}
    monkeypatch.setattr(domain.deep_research, "get_gemini_api_key", lambda: "fixture")
    monkeypatch.setattr(nano_banana, "create_study_panel", lambda *a, **kw: None)
    monkeypatch.setattr(nano_banana, "generate_figure", lambda *a, **kw: pytest.fail("unexpected fallback image"))
    with pytest.raises(RuntimeError, match="No source-grounded study panel"):
        project.invoke("generate_concept_figure", args)
    with pytest.raises(RuntimeError, match="ambiguous"):
        project.invoke("generate_concept_figure", args)
    assert "concept" not in project.state

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


def test_plan_result_path_records_same_receipt_as_legacy_expected(tmp_path, monkeypatch):
    project = open_project(tmp_path, monkeypatch)
    old = plan()
    current = {k: v for k, v in old.items() if k != "expected"}
    current["result_path"] = old["expected"]
    assert project.plan_experiment(current) == old
    assert project.plan_experiment(old) == old
    with pytest.raises(ValueError, match="Conflicting output filenames"):
        project.plan_experiment(dict(current, expected="results/else.json"))
    with pytest.raises(ValueError, match="result_path must be one output file path"):
        project.plan_experiment(dict(current, result_path="hypothesized improvement"))
    placeholders = dict(current, baseline=current["script"], comparison=current["script"], controls=[current["script"]])
    with pytest.raises(ValueError, match="baseline, comparison, controls"):
        project.plan_experiment(placeholders)
    assert project.state["plan"] == old
    mechanism = dict(explanation="Greater uptake reduces losses.", alternative="Reduced evaporation lowers losses.",
                     distinguishing_observation="Measure uptake and evaporation separately in both conditions.",
                     claim_without_it="Loss differs under treatment.")
    project.plan_experiment(dict(current, mechanism=mechanism))
    assert yaml.safe_load((project.state_dir / "experiment_plan.yaml").read_text())["mechanism"] == mechanism
    assert domain.NativeProject(tmp_path).state["plan"]["mechanism"] == mechanism
    for invalid in (None, {}, {**mechanism, "alternative":mechanism["explanation"]}):
        with pytest.raises(ValueError):
            project.plan_experiment(dict(current, mechanism=invalid))
    assert project.state["plan"]["mechanism"] == mechanism


@pytest.mark.parametrize(("reason", "expected"), [("owner_error", "running"), ("quota", "running"), ("ethics", "paused")])
def test_one_continue_after_stopping_an_already_paused_owner(tmp_path, monkeypatch, reason, expected):
    project = open_project(tmp_path, monkeypatch)
    project.state.update(state="paused", pause_reason=reason)
    project.save()
    project.invoke("stop", {})
    reopened = domain.NativeProject(tmp_path)
    assert reopened.invoke("continue_project", {})["state"] == expected
    assert domain.NativeProject(tmp_path).state["state"] == expected


def test_compiler_reports_first_source_error_instead_of_memory_statistics(tmp_path):
    log = tmp_path / "main.log"
    log.write_text(
        "./main.tex:56: Argument of \\caption has an extra }.\n"
        "<inserted text>\n  \\par\nl.56 ... caption ...\n"
        "\n 75i,7n,79p,1001b,630s stack positions out of 10000i\n"
        "./main.tex:56:  ==> Fatal error occurred, no output PDF file produced!\n"
    )
    detail = domain.extract_latex_errors(log)
    assert "./main.tex:56: Argument" in detail
    assert "stack positions" not in detail and "Fatal error" not in detail


def test_registered_paired_summary_recomputes_printed_uncertainty(tmp_path, monkeypatch):
    from ark import provenance

    project = open_project(tmp_path, monkeypatch)
    artifact = tmp_path / "results/paired.json"
    rows = [{"new": 1.21, "old": 0.1}, {"new": 2.21, "old": 0.3}, {"new": 3.21, "old": 0.8}]
    artifact.write_text(json.dumps({"trials": rows}))
    project.state["experiment"] = {"status": "completed", "artifact": "results/paired.json",
                                   "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    args = {"rows_pointer": "/trials", "summarize": "new", "reference": "old"}
    record = project.invoke("render_results_table", args)
    assert record["value"]["sample_sd"] == pytest.approx(1)
    assert record["paired_difference"]["mean"] == pytest.approx(1.81)
    assert record["percent_change_in_means"] == pytest.approx(452.5)
    assert project.invoke("render_results_table", args) == record
    assert len(project.state["measurement_summaries"]["recipes"]) == 1
    tex = r"\section{Results} Across all three splits the mean was $2.210 \pm 1.000$ (sample SD)."
    assert not provenance.audit(tex, tmp_path)["uncertainty_conflicts"]
    bad = tex.replace("1.000", "0.981")
    audit = provenance.audit(bad, tmp_path)
    assert "sample SD is 1" in audit["uncertainty_conflicts"][0]
    assert not provenance.verdict(audit)[0]
    assert not provenance.audit(bad.replace("sample SD", "bootstrap confidence interval"), tmp_path)["uncertainty_conflicts"]
    artifact.write_text(json.dumps({"trials": list(reversed(rows))}))
    assert not provenance._registered_summaries(tmp_path, None)
    with pytest.raises(ValueError, match="receipt"):
        project.invoke("render_results_table", args)


def test_condition_summary_matches_independent_view_without_whitelisting_numbers(tmp_path, monkeypatch):
    from ark import provenance, review

    host = open_project(tmp_path, monkeypatch)
    path = "results/calibration.json"
    cases = [(1, "test", 3, -7), (1, "train", -8, -4), (2, "test", 2, 0),
             (2, "train", 0, 0), (3, "test", -3, 3), (3, "train", 0, 5)]
    rows = [{"run": run, "data": {"split": split}, "residual": {"fit": fit, "base": base}}
            for run, split, fit, base in cases]
    artifact = tmp_path / path
    artifact.write_text(json.dumps({"trials": rows}))
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    host.state["experiment"] = {"status": "completed", "artifact": path, "sha256": digest}
    args = {"rows_pointer": "/trials", "summarize": "residual.fit", "reference": "residual.base"}
    legacy = host.invoke("render_results_table", args)
    result = host.invoke("render_results_table", {**args, "condition_columns": ["run", "data.split"]})
    comparisons = result.pop("per_condition")
    assert result == legacy
    assert [r["condition"] for r in comparisons] == [{"run": c[0], "data.split": c[1]} for c in cases]
    assert [r["signed_difference"] for r in comparisons] == [10, -4, 2, 0, -6, -5]
    assert [r["reference_magnitude"] for r in comparisons] == [7, 4, 0, 0, 3, 5]
    assert [r["fraction_reduction_in_magnitude"] for r in comparisons] == [4 / 7, -1, None, None, 0, 1]
    assert len(host.state["measurement_summaries"]["recipes"]) == 1
    audit = provenance.audit(r"\section{Results} An unrelated quantity was 0.571429.", tmp_path)
    assert [r["number"] for r in audit["untraced"]] == ["0.571429"]
    with pytest.raises(ValueError, match="uniquely"):
        host.invoke("render_results_table", {**args, "condition_columns": ["run"]})

    host.snapshot = review.build_snapshot(tmp_path)
    request = {"revision": host.snapshot["revision"], "artifact": path, "rows_pointer": "/trials",
               "statistic": "paired", "column": "residual.fit", "reference": "residual.base",
               "condition_columns": ["run", "data.split"]}
    monkeypatch.setattr(review, "VIEW_SIZE", 450)
    first = host.invoke("review_evidence", request)
    lines = first["text"].splitlines()
    view = first
    while view["next_start"]:
        view = host.invoke("review_evidence", {**request, "start": view["next_start"]})
        lines.extend(view["text"].splitlines())
    assert [json.loads(line) for line in lines] == comparisons
    assert all(r["sha256"] == digest and r["revision"] == request["revision"] for r in host.review_receipts)
    cite = {"artifact": path, "quote": lines[0]}
    verified = review._references(host.snapshot, [cite], [first["receipt"]], "", required=True)
    assert review._cited_measurement_columns(host.snapshot, verified) == {"residual.fit", "residual.base"}
    subjects = [{"subject": name, "column": col, "condition": {"column": "data.split", "value": "test"}}
                for name, col in (("fitted", "residual.fit"), ("baseline", "residual.base"))]
    binding = review._comparison_bindings(host.snapshot, {"comparison": subjects}, {"quote": "fitted vs baseline"},
                                          verified, ["residual.fit", "residual.base"])
    assert binding["comparison"] == subjects
    for quote in (lines[-1], lines[0].replace('"test"', '"other"'), "0.5714"):
        with pytest.raises(ValueError, match="Unobserved"):
            review._references(host.snapshot, [{**cite, "quote": quote}], [first["receipt"]], "", required=True)
    artifact.write_text("[]")
    with pytest.raises(ValueError, match="changed"):
        host.invoke("review_evidence", request)
