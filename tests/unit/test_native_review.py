import copy
import json
from pathlib import Path

import fitz
import pytest

from ark.review import build_snapshot, read_evidence, record_is_current, review_prompt, validate_review

PAIRS = json.loads((Path(__file__).parents[2] / "test_fixtures/peer_review/pairs.json").read_text())


def project(tmp_path, pair, variant):
    paper = tmp_path / "paper"
    paper.mkdir(exist_ok=True)
    (paper / "main.tex").write_text(pair[variant])
    for folder, name, key in (("results", "rows.csv", "table"), ("sources", "study.txt", "source")):
        if key in pair:
            path = tmp_path / folder / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(pair[key])
    return build_snapshot(tmp_path, required=["manuscript", pair["domain"]])


def record(snapshot, pair, variant, status):
    field = pair["domain"]
    manuscript = {"artifact": "paper/main.tex", "quote": pair[variant]}
    path = "results/rows.csv" if field == "experiment" else "sources/study.txt"
    view = read_evidence(snapshot, path)
    observation = {"artifact": path, "quote": view["text"].strip()}
    checks = {d: {"status": "unassessed", "reason": "Additional evidence was not available.", "evidence": []}
              for d in ("manuscript", "experiment", "citations", "visual")}
    for domain, ref in (("manuscript", manuscript), (field, observation)):
        checks[domain] = {"status": "assessed", "reason": "Compared this passage to the recorded evidence.", "evidence": [ref]}
    issues = [] if not status else [{
        "id": "R1", "status": status, "domain": field, "severity": "blocker", "claim": pair[variant],
        "reason": pair["defect"], "fix": pair["clean"], "evidence": [manuscript, observation]}]
    return {"revision": snapshot["revision"], "summary": pair["question"], "alternative": pair["defect"],
            "strengths": ["The observation can be inspected directly."], "checks": checks, "issues": issues,
            "verdict": "revise" if status in ("new", "still_present") else "accept"}, [view["receipt"]]


@pytest.mark.parametrize("pair", PAIRS, ids=lambda p: p["topic"])
def test_paired_lifecycle_and_sham_fix(tmp_path, pair):
    snapshot = project(tmp_path, pair, "flawed")
    response, receipts = record(snapshot, pair, "flawed", "new")
    before = validate_review(snapshot, response, observed=receipts)
    assert before["verdict"] == "revise" and not before["deliverable"]
    assert record_is_current(build_snapshot(tmp_path, required=snapshot["required"]), before)
    p = tmp_path / "paper/main.tex"
    p.write_text(p.read_text() + "\nA spelling change elsewhere.")
    sham = build_snapshot(tmp_path, required=snapshot["required"])
    response, receipts = record(sham, pair, "flawed", "resolved")
    with pytest.raises(ValueError, match="No changed observation"):
        validate_review(sham, response, before, observed=receipts)
    after = project(tmp_path, pair, "clean")
    assert not record_is_current(after, before)
    response, receipts = record(after, pair, "clean", "resolved")
    assert validate_review(after, response, before, observed=receipts)["deliverable"]
    response["issues"] = []
    assert validate_review(after, response, observed=receipts)["deliverable"]
    with pytest.raises(ValueError, match="Recheck every"):
        validate_review(after, response, before, observed=receipts)


def test_review_feedback_returns_independent_repairs_without_accepting_the_draft(tmp_path):
    from ark.review import draft_evidence_feedback

    pair = PAIRS[0]
    snap = project(tmp_path, pair, "flawed")
    response, observed = record(snap, pair, "flawed", "new")
    response["checks"]["manuscript"]["evidence"] = [{"artifact": "paper/main.tex", "quote": "Text absent from draft."}]
    response["checks"]["experiment"]["evidence"] = [{"artifact": "results/rows.csv", "quote": "Invented row."}]
    response["issues"][0]["evidence"] = [{"artifact": "results/rows.csv",
                                         "quote": read_evidence(snap, "results/rows.csv")["text"]}]
    original = copy.deepcopy(response)
    with pytest.raises(ValueError) as rejected:
        validate_review(snap, response, observed=observed)
    feedback = draft_evidence_feedback(snap, response, observed, str(rejected.value))
    assert "paper/main.tex" in feedback and "checks.experiment" in feedback and "issues[0]" in feedback
    assert response == original
    with pytest.raises(ValueError):
        validate_review(snap, response, observed=observed)


def test_pdf_quote_retry_hint_requires_a_unique_observed_literal_window(tmp_path):
    from ark import review

    pair = PAIRS[0]
    project(tmp_path, pair, "clean")
    artifact = "paper/main.pdf"
    quote = "unique result"
    with fitz.open() as doc:
        for text in ("Cited page", f"Prefix {quote}", f"Prefix {quote}"):
            doc.new_page().insert_text((60, 65), text)
        doc.save(tmp_path / artifact)
    snap = build_snapshot(tmp_path)
    response, observed = record(snap, pair, "clean", "")
    ref = {"artifact": artifact, "page": 1, "quote": quote}
    response["checks"]["manuscript"]["evidence"] = [ref]
    cited_text = read_evidence(snap, artifact, page=1)["receipt"]
    cited_pixels = read_evidence(snap, artifact, page=1, visual=True)["receipt"]
    partial = read_evidence(snap, artifact, page=2, start=10)["receipt"]
    full = read_evidence(snap, artifact, page=2, start=8)["receipt"]
    other = read_evidence(snap, artifact, page=3, start=8)["receipt"]
    visual = read_evidence(snap, artifact, page=2, visual=True)["receipt"]

    def feedback(*additional):
        receipts = [*observed, *additional]
        with pytest.raises(ValueError, match="Unobserved") as rejected:
            validate_review(snap, response, observed=receipts)
        return review.draft_evidence_feedback(snap, response, receipts, str(rejected.value))

    pixels_only = feedback(cited_pixels)
    assert "Only pixels were observed on page 1" in pixels_only
    assert 'read_evidence(artifact="paper/main.pdf", page=1, visual=false)' in pixels_only
    assert "Only pixels" not in feedback(dict(cited_pixels, revision="old"))
    observed.append(cited_text)
    assert "Quote the delivered text of page 1 verbatim" in feedback(cited_pixels)
    assert "observed page 2" in feedback(partial, full, full)
    for receipts in ((), (full, other), (partial,), (visual,), (dict(full, sha256="old"),),
                     (dict(full, revision="old"),), (dict(full, artifact="sources/other.pdf"),),
                     (dict(full, end=full["end"] - 1),)):
        assert "Retry the quote" not in feedback(*receipts)
    ref["quote"] = "unique  result"
    assert "Retry the quote" not in feedback(full)
    ref.update(page=2, quote=quote)
    assert validate_review(snap, response, observed=[*observed, full])["verdict"] == "accept"


def test_blind_packet_contains_no_private_values_or_score(tmp_path):
    pair = PAIRS[0]
    snapshot = project(tmp_path, pair, "flawed")
    (tmp_path / "config.yaml").write_text("provider_password: TOP_SECRET")
    (tmp_path / "results/tokens.json").write_text("TOP_SECRET")
    (tmp_path / "auto_research/state").mkdir(parents=True)
    (tmp_path / "auto_research/state/latest_review.md").write_text("PRIOR_SCORE_9")
    packet = review_prompt(build_snapshot(tmp_path))
    for hidden in (pair["table"], "TOP_SECRET", str(tmp_path), "PRIOR_SCORE_9"):
        assert hidden not in packet
    assert "results/rows.csv" in packet
    with pytest.raises(ValueError, match="outside"):
        read_evidence(snapshot, "config.yaml")
    with pytest.raises(ValueError, match="relative"):
        read_evidence(snapshot, "../secret")


def test_conclusion_reviewer_checks_the_whole_observed_paragraph_against_results(tmp_path):
    pair = {"domain": "experiment", "clean": "Table 1 reports loss in treated and control plots.",
            "table": "trial,treated_loss,control_loss\n1,2.1,3.5\n2,2.2,3.1\n",
            "question": "Does treatment reduce loss?", "defect": "Root uptake was not recorded."}
    project(tmp_path, pair, "clean")
    pdf = tmp_path / "paper/main.pdf"
    effect = "The treated plots had lower losses in these trials."
    mechanism = "Improved root uptake explains the difference."

    def paper(passage, heading="Conclusion"):
        with fitz.open() as doc:
            doc.new_page().insert_text((40, 40), "Table 1 reports mean losses.")
            page = doc.new_page()
            page.insert_text((40, 40), heading)
            page.insert_textbox((40, 65, 340, 240), passage)
            doc.save(pdf)
        return build_snapshot(tmp_path, required=["experiment"])

    def assess(snap, level):
        answer, observed = record(snap, pair, "clean", "")
        target = snap["conclusion"]
        page = read_evidence(snap, target["artifact"], page=target["page"], start=target["start"])
        assert page["text"].startswith(target["quote"])
        ref = {k: target[k] for k in ("artifact", "page", "quote")}
        result = answer["checks"]["experiment"]["evidence"][0]
        reason = {"measured": "Loss is lower in both observed pairs.",
                  "interpretation": "The comparison measures loss. The paper offers uptake as a possibility.",
                  "needs_evidence": "The losses are observed but the study does not measure root uptake."}
        answer["checks"]["manuscript"].update(evidence=[ref, result], conclusion_support=level, reason=reason[level])
        return answer, [*observed, page["receipt"]]

    snap = paper(effect + " " + mechanism)
    assert mechanism in " ".join(snap["conclusion"]["quote"].split())
    original, seen = record(snap, pair, "clean", "")
    proposal, observed = assess(snap, "needs_evidence")
    with pytest.raises(ValueError, match="conclusion paragraph"):
        validate_review(snap, original, observed=observed)  # Page seen, assessed only the original result.
    with pytest.raises(ValueError, match="Unobserved"):
        validate_review(snap, proposal, observed=seen)
    with pytest.raises(ValueError, match="open blocker"):
        validate_review(snap, proposal, observed=observed)
    sham = copy.deepcopy(proposal)
    sham["checks"]["manuscript"].update(conclusion_support="measured")
    sham["checks"]["manuscript"]["evidence"][0]["quote"] = effect
    with pytest.raises(ValueError, match="conclusion paragraph"):
        validate_review(snap, sham, observed=observed)

    issue = dict(id="R1", status="new", severity="blocker", domain="manuscript", claim=mechanism,
                 reason="The rows measure losses only.", fix="Measure uptake or qualify the explanation.",
                 evidence=proposal["checks"]["manuscript"]["evidence"])
    proposal.update(issues=[issue], verdict="revise")
    rejected = validate_review(snap, proposal, observed=observed)
    assert rejected["verdict"] == "revise"
    (tmp_path / "paper/main.tex").write_text(pair["clean"] + "\nChanged punctuation elsewhere.")
    unchanged = build_snapshot(tmp_path, required=["experiment"])
    proposal, observed = assess(unchanged, "measured")
    proposal["issues"] = [dict(issue, status="resolved")]
    with pytest.raises(ValueError, match="No changed observation"):
        validate_review(unchanged, proposal, rejected, observed=observed)

    for passage, support in ((effect, "measured"), (effect + " Root uptake might explain it.", "interpretation")):
        clean = paper(passage)
        proposal, observed = assess(clean, support)
        proposal["issues"] = [dict(issue, status="resolved", evidence=proposal["checks"]["manuscript"]["evidence"])]
        assert validate_review(clean, proposal, rejected, observed=observed)["deliverable"]

    assert "conclusion" not in paper(effect, "Results")
    (tmp_path / "results/rows.csv").unlink()
    assert "conclusion" not in paper(effect)


def test_observed_quote_and_current_revision_required(tmp_path):
    pair = PAIRS[0]
    snap = project(tmp_path, pair, "flawed")
    response, receipts = record(snap, pair, "flawed", "new")
    with pytest.raises(ValueError, match="Unobserved"):
        validate_review(snap, response)
    fake = copy.deepcopy(response)
    fake["checks"]["experiment"]["evidence"][0]["quote"] = "never observed"
    with pytest.raises(ValueError, match="Unobserved"):
        validate_review(snap, fake, observed=receipts)
    response["verdict"] = "accept"
    response["score"] = 10
    with pytest.raises(ValueError, match="verdict disagrees"):
        validate_review(snap, response, observed=receipts)
    (tmp_path / "results/rows.csv").write_text("split,mean_loss,n\n1,2,3")
    new = build_snapshot(tmp_path)
    with pytest.raises(ValueError, match="current revision"):
        validate_review(new, response, observed=receipts)
    with pytest.raises(ValueError, match="changed"):
        read_evidence(snap, "results/rows.csv")


@pytest.mark.parametrize("score", [-1, 11, True, "8/10", float("nan"), float("inf")])
def test_reject_invalid_optional_score(tmp_path, score):
    pair = PAIRS[0]
    snapshot = project(tmp_path, pair, "clean")
    response, receipts = record(snapshot, pair, "clean", "")
    response["score"] = score
    with pytest.raises(ValueError, match="finite number"):
        validate_review(snapshot, response, observed=receipts)


def test_unobserved_required_evidence_abstains(tmp_path):
    pair = PAIRS[0]
    snapshot = project(tmp_path, pair, "clean")
    response, _ = record(snapshot, pair, "clean", "")
    response["checks"]["experiment"] = {"status": "unassessed", "reason": "The measurements were withheld.", "evidence": []}
    response["verdict"] = "unassessed"
    assert not validate_review(snapshot, response)["deliverable"]


def test_visual_evidence_is_actual_rendered_current_pages(tmp_path):
    pair = PAIRS[0]
    project(tmp_path, pair, "clean")
    with fitz.open() as doc:
        for text in ("First panel", "Second panel"):
            doc.new_page().insert_text((60, 65), text)
        doc.save(tmp_path / "paper/main.pdf")
    snap = build_snapshot(tmp_path, required=["visual"])
    response, receipts = record(snap, pair, "clean", "")
    p = "paper/main.pdf"
    text = read_evidence(snap, p, page=1)
    assert "First panel" in text["text"]
    response["checks"]["visual"] = {"status": "assessed", "reason": "Viewed both panels.",
                                    "evidence": [{"artifact": p, "page": 1, "visual": True}]}
    with pytest.raises(ValueError, match="pixels"):
        validate_review(snap, response, observed=[*receipts, text["receipt"]])
    first = read_evidence(snap, p, page=1, visual=True)
    assert first["mime_type"] == "image/png" and first["image_base64"].startswith("iVBOR")
    with pytest.raises(ValueError, match="every manuscript page"):
        validate_review(snap, response, observed=[*receipts, first["receipt"]])
    second = read_evidence(snap, p, page=2, visual=True)
    assert validate_review(snap, response, observed=[*receipts, first["receipt"], second["receipt"]])["deliverable"]


def test_review_uses_executed_code_and_requested_quantity_across_a_source_edit(tmp_path):
    import hashlib

    pair = PAIRS[0]
    project(tmp_path, pair, "clean")
    brief = "auto_research/state/idea.md"
    (tmp_path / brief).parent.mkdir(parents=True)
    (tmp_path / brief).write_text("Measure comparisons per trial.")
    script = "scripts/measure.py"
    code = "def per_trial(total, trials):\n    return total / trials\nratio = per_trial(total_comparisons, trials)"
    (tmp_path / script).parent.mkdir()
    (tmp_path / script).write_text(code)
    token = "a" * 24
    run = dict(status="completed", script=script, code_sha256=hashlib.sha256(code.encode()).hexdigest(),
               identity="i2p-" + token)
    snapshot = build_snapshot(tmp_path, measurement_run=run)
    assert snapshot["measurement_method"]["artifact"] == script
    response, receipts = record(snapshot, pair, "clean", "")
    with pytest.raises(ValueError, match="executed calculation"):
        validate_review(snapshot, response, observed=receipts)
    for key in (script, brief):
        reading = read_evidence(snapshot, key)
        response["checks"]["experiment"]["evidence"].append({"artifact": key, "quote": (tmp_path / key).read_text()})
        receipts.append(reading["receipt"])
    method_ref = next(r for r in response["checks"]["experiment"]["evidence"] if r["artifact"] == script)
    method_ref["quote"] = "ratio = per_trial(total_comparisons, trials)"
    with pytest.raises(ValueError, match=r"per_trial \(line 1\)"):
        validate_review(snapshot, response, observed=receipts)
    method_ref["quote"] = "return total / trials"
    assert validate_review(snapshot, response, observed=receipts)["verdict"] == "accept"
    (tmp_path / script).write_text("# A later revision")
    assert build_snapshot(tmp_path, measurement_run=run)["measurement_method"]["artifact"] is None
    sealed = f"auto_research/state/native_runs/{token}/input/main.py"
    (tmp_path / sealed).parent.mkdir(parents=True)
    (tmp_path / sealed).write_text(code)
    assert build_snapshot(tmp_path, measurement_run=run)["measurement_method"]["artifact"] == sealed


def test_known_answer_experiment_control_is_bound_to_worker_and_verdict(tmp_path):
    import hashlib

    pair = PAIRS[0]
    project(tmp_path, pair, "clean")
    method = "scripts/metric.py"
    source = "def metric(a, b):\n    return a / b"
    (tmp_path / "scripts").mkdir()
    (tmp_path / method).write_text(source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    run = dict(status="completed", backend="slurm", script=method, code_sha256=digest)
    snap = build_snapshot(tmp_path, measurement_run=run)
    response, receipts = record(snap, pair, "clean", "")
    reading = read_evidence(snap, method)
    receipts.append(reading["receipt"])
    response["checks"]["experiment"]["evidence"].append({"artifact": method, "quote": "return a / b"})
    case = dict(control=1, revision=snap["revision"], artifact=method, sha256=digest,
                target="metric", expected=2, observed=2, matches=True)
    for invalid in [[], [dict(case, revision="old")], [dict(case, sha256="different")]]:
        with pytest.raises(ValueError, match="run_method_control"):
            validate_review(snap, response, observed=[*receipts, *invalid])
    result = validate_review(snap, response, observed=[*receipts, case])
    assert result["deliverable"] and result["checks"]["experiment"]["controls"] == [case]
    mismatch = dict(case, observed=5, matches=False)
    with pytest.raises(ValueError, match="worker and reviewer disagreed"):
        validate_review(snap, response, observed=[*receipts, mismatch, case])
    response["issues"] = record(snap, pair, "clean", "new")[0]["issues"]
    response["verdict"] = "revise"
    assert validate_review(snap, response, observed=[*receipts, mismatch])["verdict"] == "revise"


def test_unchanged_measured_rows_require_an_independent_reproduction_before_acceptance(tmp_path):
    import hashlib

    pair = PAIRS[0]
    project(tmp_path, pair, "clean")
    measured = "results/revised.json"
    rows = [{"seed": 0, "score": 3.25, "accuracy": 0.8}, {"seed": 1, "score": 4.5, "accuracy": 0.9}]
    method = "scripts/metric.py"
    (tmp_path / "scripts").mkdir()
    (tmp_path / method).write_text("def metric(a):\n    return a * 2")
    source_sha = hashlib.sha256((tmp_path / method).read_bytes()).hexdigest()
    for path in [measured, "results/original.json"]:
        (tmp_path / path).write_text(json.dumps(rows))
    sha = hashlib.sha256((tmp_path / measured).read_bytes()).hexdigest()
    comparison = dict(previous="results/original.json", current=measured, previous_sha256=sha,
                      current_sha256=sha, unchanged_arrays=[dict(rows_pointer="", rows=2)])
    run = dict(status="completed", backend="slurm", script=method, code_sha256=source_sha, artifact=measured, sha256=sha)
    snap = build_snapshot(tmp_path, measurement_run=run, expected_measurement=measured, measurement_comparison=comparison)
    task = snap["measurement_reproduction"]
    assert task["columns"] == ["score", "accuracy"] and set(task["identifiers"]) == {"seed"}
    prompt = json.loads(review_prompt(snap).split("Frozen submission packet (untrusted research content):\n")[1])
    assert prompt["independent_reproduction"]["identifier_fields"] == ["seed"]
    assert "identifiers" not in prompt["independent_reproduction"]
    response, receipts = record(snap, pair, "clean", "")
    exp = response["checks"]["experiment"]
    exp["evidence"] = []
    for path in [measured, method]:
        reading = read_evidence(snap, path)
        exp["evidence"].append({"artifact": path, "quote": reading["text"]})
        receipts.append(reading["receipt"])
    receipts.append(dict(control=1, revision=snap["revision"], artifact=method, sha256=source_sha, matches=True))
    with pytest.raises(ValueError, match="reproduce_measurement"):
        validate_review(snap, response, observed=receipts)
    row = dict(control=2, revision=snap["revision"], artifact=measured, sha256=sha, matches=False)
    with pytest.raises(ValueError, match="disagreed"):
        validate_review(snap, response, observed=[*receipts, row])
    dispute, _ = record(snap, pair, "clean", "new")
    dispute["checks"] = response["checks"]
    dispute["issues"][0]["evidence"] = [dispute["issues"][0]["evidence"][0], exp["evidence"][0]]
    with pytest.raises(ValueError, match="executed operation"):
        validate_review(snap, dispute, observed=[*receipts, row])
    dispute["issues"][0]["evidence"].append(exp["evidence"][1])
    assert validate_review(snap, dispute, observed=[*receipts, row])["verdict"] == "revise"
    assert validate_review(snap, response, observed=[*receipts, dict(row, matches=True)])["deliverable"]
    assert validate_review(snap, response, observed=[*receipts[:-1], dict(row, matches=True)])["deliverable"]

    from ark.orchestrator.native_domain import NativeProject
    from ark import review
    from ark.compute import review_control
    from unittest.mock import patch
    blind = review.reproduction_brief(snap)
    assert "outcome_columns" in blind and "return a * 2" not in blind and "4.5" not in blind
    assert "boundary case with a known answer" in blind and "final reporting" in blind
    assert "Your own program can also be wrong" in prompt["reproduction_instructions"]
    host = NativeProject(tmp_path)
    host.snapshot = snap
    host.reproduction_decision = None
    host.reproduction_path = tmp_path / "sealed.json"
    host.reproduction_attempts = 0
    host.reproduction_format_retry = False
    host.state_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match="Before opening"):
        host.review_evidence({"revision": snap["revision"], "artifact": measured})
    with pytest.raises(ValueError, match="Before opening"):
        host.run_method_control({"revision": snap["revision"]})
    assert '"seed": "int"' in blind and 'a flat Python dict' in blind
    # Execution failures use the regular budget. A completed calculation with a
    # malformed row permits exactly one extra attempt while evidence stays hidden.
    probe = {"revision": snap["revision"], "program": "def reproduce(ids): return []"}
    failures = [ValueError("calculation failed"), review_control.ReproductionFormatError("Single row required"),
                review_control.ReproductionFormatError("Single row required")]
    with patch.object(review_control, "reproduce", side_effect=failures) as runner:
        for phrase in ("calculation failed", "Remaining executions: 2", "Remaining executions: 1"):
            with pytest.raises(ValueError, match=phrase):
                host.reproduce_measurement(probe)
        assert runner.call_count == 3 and host.reproduction_decision is None
    original_program = 'def reproduce(identifiers): return {"score":7,"accuracy":0.9}'
    with patch.object(review_control, "reproduce", return_value={"score":7,"accuracy":0.9}) as worker:
        request = {"revision": snap["revision"], "program": original_program}
        sealed = host.reproduce_measurement(request)
        assert host.reproduce_measurement(request) == sealed
        assert worker.call_count == 1 and not sealed["receipt"]["matches"]
        assert host.review_evidence({"revision": snap["revision"], "artifact": measured})["receipt"]
        host.reproduction_decision = None  # model transport reconnects to the same durable commitment
        with pytest.raises(ValueError, match="sealed"):
            host.reproduce_measurement(request | {"program": "def reproduce(ids): return {}"})
        assert worker.call_count == 1 and json.loads(host.reproduction_path.read_text()) == sealed
    host.reproduction_path = tmp_path / "abstained.json"
    host.reproduction_decision = None
    with patch.object(review_control, "reproduce") as runner:
        with pytest.raises(ValueError, match="attempts exhausted"):
            host.reproduce_measurement(probe)
        runner.assert_not_called()
    assert host.reproduce_measurement({"revision": snap["revision"], "unavailable": "Missing offline dataset"}) == {
        "unavailable": "Missing offline dataset"}
    with pytest.raises(ValueError, match="sealed"):
        host.reproduce_measurement(request)


def test_control_can_only_measure_the_frozen_calculation(monkeypatch):
    from ark.compute import review_control

    source = b"import math\ndef metric(a, b):\n    return math.log(a) / b\n"
    runtime = {"backend": "slurm"}
    args = dict(target="math.log(a) / b", inputs_json='{"a":1,"b":2}', expected_json="0",
                alternative_json="0.5", rationale="Dividing the value itself would give a half.")
    executions = []
    def observe(backend, image, payload):
        executions.append(payload)
        return 0.0
    monkeypatch.setattr(review_control, "_execute", observe)
    assert review_control.run(source, runtime, args)["matches"]
    assert executions[0]["source"] == source.decode() and not executions[0]["function"]
    for changed in [dict(alternative_json="0"), dict(target="math.log(a) + b"), dict(inputs_json='{"math":0}'),
                    dict(inputs_json='{"unrelated":1}'), dict(expected_json='"not a number"')]:
        with pytest.raises(ValueError):
            review_control.run(source, runtime, args | changed)
    assert len(executions) == 1


def test_isolated_control_driver_measures_unmodified_method_and_inline_expression(tmp_path):
    import subprocess
    import sys
    from ark.compute.review_control import DRIVER

    source = ("import math\ndef metric(a, b):\n    return math.floor(a / b)\n"
              "if __name__ == '__main__':\n    raise RuntimeError('full experiment launched')\n")
    for target, function, expected in (("metric", True, 1), ("math.floor(a / b)", False, 1)):
        case = dict(source=source, target=target, function=function, inputs={"a": 3, "b": 2}, array_inputs=[])
        result = subprocess.run([sys.executable, "-I", "-B", "-c", DRIVER], cwd=tmp_path,
                                input=json.dumps(case), capture_output=True, text=True, check=True, timeout=5)
        assert json.loads(result.stdout) == expected
    assert not list(tmp_path.iterdir())


def test_revised_plan_can_compare_old_rows_but_must_assess_current_output(tmp_path):
    import hashlib

    pair = PAIRS[0]
    project(tmp_path, pair, "clean")
    old = "results/rows.csv"
    current = "results/revised.json"
    rows = '[{"trials": 8}]'
    (tmp_path / current).write_text(rows)
    run = {"status": "completed", "artifact": current, "sha256": hashlib.sha256(rows.encode()).hexdigest()}
    snap = build_snapshot(tmp_path, expected_measurement=current, measurement_run=run)
    assert snap["artifacts"][old]["kind"] == "supporting_measurement"
    assert snap["artifacts"][current]["kind"] == "measurement"
    response, receipts = record(snap, pair, "clean", "")
    with pytest.raises(ValueError, match="current worker output results/revised.json"):
        validate_review(snap, response, observed=receipts)
    response["checks"]["experiment"]["evidence"].append({"artifact": current, "quote": rows})
    receipts.append(read_evidence(snap, current)["receipt"])
    assert validate_review(snap, response, observed=receipts)["deliverable"]
    for receipt in (None, {**run, "artifact": old}, {**run, "sha256": "0" * 64}):
        invalid = build_snapshot(tmp_path, expected_measurement=current, measurement_run=receipt)
        assert not invalid["worker_measurement"]["verified"]
        assert invalid["revision"] != snap["revision"]
        assert all(meta["kind"] != "measurement" for meta in invalid["artifacts"].values())
        invalid_response, observed = record(invalid, pair, "clean", "")
        with pytest.raises(ValueError, match="no verified worker output"):
            validate_review(invalid, invalid_response, observed=observed)


def test_review_of_measured_figure_needs_plotted_source_rows_and_pixels(tmp_path):
    from PIL import Image

    pair = PAIRS[0]
    project(tmp_path, pair, "clean")
    paper = tmp_path / "paper"
    include = r"\includegraphics{figures/measured.png}"
    (paper / "main.tex").write_text(pair["clean"] + "\n" + r"\input{results}" + "\n" + include)
    (paper / "results.tex").write_text(r"\includegraphics{figures/measured}")
    (paper / "unused.tex").write_text(r"\includegraphics{only_in_draft.png}")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    method = "scripts/visualize.py"
    snippet = "axis.plot(sizes, counts)"
    (tmp_path / method).write_text(snippet + '\nfigure.savefig("measured.png")')
    (scripts / "other.py").write_text('fig.savefig("only_in_draft.png")')
    (paper / "figures").mkdir()
    art = paper / "figures/measured.png"
    Image.new("RGB", (100, 50), "#358b70").save(art)
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((60, 65), "Measured counts")
        page.insert_image((60, 75, 260, 175), filename=str(art))
        pdf.save(paper / "main.pdf")
    snapshot = build_snapshot(tmp_path, required=["visual"])
    assert snapshot["figure_methods"] == {"measured.png": method}
    assert "checks.visual.evidence" in review_prompt(snapshot)
    response, receipts = record(snapshot, pair, "clean", "")
    visual = {"artifact": "paper/main.pdf", "page": 1, "visual": True}
    seen = read_evidence(snapshot, visual["artifact"], page=1, visual=True)
    receipts = [*receipts, seen["receipt"], read_evidence(snapshot, method)["receipt"]]
    check = response["checks"]["visual"]
    check.update(status="assessed", reason="Compared the measured series.", evidence=[visual])
    with pytest.raises(ValueError, match="Trace the plotted values"):
        validate_review(snapshot, response, observed=receipts)
    check["evidence"].append({"artifact": method, "quote": snippet})
    with pytest.raises(ValueError, match="measurement excerpt"):
        validate_review(snapshot, response, observed=receipts)
    check["evidence"].extend(response["checks"]["experiment"]["evidence"])
    with pytest.raises(ValueError, match="series_origin"):
        validate_review(snapshot, response, observed=receipts)
    check["series_origin"] = {"measured.png": "measured"}
    clean = validate_review(snapshot, response, observed=receipts)
    assert clean["deliverable"] and record_is_current(snapshot, clean)
    del clean["checks"]["visual"]["series_origin"]
    assert not record_is_current(snapshot, clean)
    check["series_origin"]["measured.png"] = "copied"
    with pytest.raises(ValueError, match="reproducibility blocker"):
        validate_review(snapshot, response, observed=receipts)
    response["issues"] = [{"id":"R1", "status":"new", "severity":"blocker", "domain":"visual",
                           "claim":"Figure 1", "reason":"Values are copied into the program.",
                           "fix":"Use the measured inputs.", "evidence":check["evidence"]}]
    response["verdict"] = "revise"
    assert not validate_review(snapshot, response, observed=receipts)["deliverable"]
    refs = [{"artifact": method, "quote": snippet}, {"artifact":"paper/main.tex", "quote":include}]
    response["issues"][0]["evidence"] = refs
    located = validate_review(snapshot, response, observed=receipts)["issues"][0]["evidence"]
    assert located[-1]["page"] == 1 and located[-1]["image_sha256"] == seen["receipt"]["image_sha256"]
    refs[1] = {"artifact":"paper/main.tex", "quote":pair["clean"]}
    with pytest.raises(ValueError, match="Missing visual observation"):
        validate_review(snapshot, response, observed=receipts)
    (scripts / "alternative.py").write_text('fig.savefig("measured.png")')
    assert "figure_methods" not in build_snapshot(tmp_path, required=["visual"])
    assert build_snapshot(tmp_path, required=["visual"], registered_plots={"measured.png": method})["figure_methods"] == {"measured.png": method}


def test_linked_in_project_or_external_artifacts_not_followed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "paper").mkdir()
    secret = tmp_path / "private.tex"
    secret.write_text("hidden")
    (root / "paper/main.tex").symlink_to(secret)
    with pytest.raises(ValueError, match="Linked"):
        build_snapshot(root)


def test_blind_report_requires_independent_source_bound_adjudication(tmp_path):
    import hashlib
    from scripts.eval_peer_review import grade

    key = {f"opaque{i}": {"variant": variant} for i, variant in enumerate(["flawed", "clean", "withheld"])}
    jury = {}
    for ident in key:
        artifact = tmp_path / (ident + ".md")
        artifact.write_text("One blind review of " + ident)
        jury[ident] = {"note": "Checked against the recorded evidence.",
                       "review_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                       "defect_found": True, "evidence_located": True, "unsupported_block": False, "abstained": True}
    assert grade(key, tmp_path, jury)["pass"]
    jury["opaque1"]["unsupported_block"] = True
    report = grade(key, tmp_path, jury)
    assert not report["pass"] and report["metrics"]["clean_controls_incorrectly_blocked"] == {"count": 1, "total": 1}
    (tmp_path / "opaque1.md").write_text("Replacement review")
    with pytest.raises(ValueError, match="different review"):
        grade(key, tmp_path, jury)


def test_measured_recomputation_is_bound_to_rows_and_receipt(tmp_path):
    import json
    pair = PAIRS[2]
    project(tmp_path, pair, "clean")
    data = tmp_path / "results/replications.json"
    data.write_text(json.dumps({"paired": [{"ms": x} for x in (11, 10, 9)]}))
    snap = build_snapshot(tmp_path)
    response, receipts = record(snap, pair, "clean", "")
    view = read_evidence(snap, "results/replications.json", statistic="median", column="ms", rows_pointer="/paired")
    assert view["text"] == "median(ms) = 10 over 3 recorded rows"
    response["checks"]["experiment"]["evidence"] = [{"artifact": view["artifact"], "quote": view["text"]}]
    with pytest.raises(ValueError, match="Unobserved"):
        validate_review(snap, response, observed=receipts)
    proof = validate_review(snap, response, observed=[*receipts, view["receipt"]])
    assert proof["checks"]["experiment"]["evidence"][0]["rows_pointer"] == "/paired"
    csv = read_evidence(snap, "results/rows.csv", statistic="mean", column="cached_ms")
    assert csv["text"] == "mean(cached_ms) = 10 over 3 recorded rows"
    with pytest.raises(ValueError, match="missing"):
        read_evidence(snap, "results/rows.csv", statistic="median", column="missing")


def test_json_quote_uses_only_contiguous_tokens_in_one_observed_window(tmp_path, monkeypatch):
    import ark.review as review
    pair = PAIRS[0]
    project(tmp_path, pair, "clean")
    path = "results/samples.json"
    source = r'["text with an escaped \" and fake [7, 8]", {"n":1.00e+1,"label":"a  b"}, {"n":9,"label":"other"}]'
    (tmp_path / path).write_text(source)
    snapshot = build_snapshot(tmp_path)
    response, _ = record(snapshot, pair, "clean", "")
    ref = {"artifact": path}
    response["checks"]["experiment"]["evidence"] = [ref]
    monkeypatch.setattr(review, "VIEW_SIZE", len('{"n":1.00e+1,"label":"a  b"}') + 2)
    receipt = read_evidence(snapshot, path, start=source.index('{"n"') + 1)["receipt"]
    ref["quote"] = '{\n"n" : 1.00e+1,\t"label": "a  b"\n}'
    assert validate_review(snapshot, response, observed=[receipt])["deliverable"]
    with pytest.raises(ValueError, match="Unobserved"):
        validate_review(snapshot, response)
    for wrong in ['{"n":10,"label":"a  b"}', '{"n":1.00e+1,"label":"a b"}',
                  '{"label":"a  b","n":1.00e+1}', '{"n":1.00e+1,"label":"other"}', '{"n":9}']:
        ref["quote"] = wrong
        with pytest.raises(ValueError, match="Unobserved"):
            validate_review(snapshot, response, observed=[receipt])
    start = source.index("[7, 8]") + 1
    ref["quote"] = "[7,8]"
    with pytest.raises(ValueError, match="Unobserved"):
        validate_review(snapshot, response, observed=[read_evidence(snapshot, path, start=start)["receipt"]])
    ref["quote"] = '[{"n":1.00e+1,"label":"a  b"},{"n":9,"label":"other"}]'
    final = read_evidence(snapshot, path, start=source.rindex('{"n"') + 1)["receipt"]
    with pytest.raises(ValueError, match="Unobserved"):
        validate_review(snapshot, response, observed=[receipt, final])
    ref["quote"] = source.replace("}, {", "},\n{")
    windows = [read_evidence(snapshot, path, start=i)["receipt"] for i in range(1, len(source) + 1, review.VIEW_SIZE)]
    with pytest.raises(ValueError, match="Unobserved"):
        validate_review(snapshot, response, observed=windows)


def test_formatting_an_old_json_observation_cannot_resolve_a_blocker(tmp_path):
    pair = PAIRS[0]
    project(tmp_path, pair, "flawed")
    path = "results/samples.json"
    artifact = tmp_path / path
    artifact.write_text('[ [1, 2], 3 ]')
    snap = build_snapshot(tmp_path)
    response, _ = record(snap, pair, "flawed", "new")
    ref = {"artifact": path, "quote": "[1,2]"}
    response["checks"]["experiment"]["evidence"] = [ref]
    response["issues"][0]["evidence"][1] = ref
    original = validate_review(snap, response, observed=[read_evidence(snap, path)["receipt"]])
    artifact.write_text('[ [ 1, 2 ], 4 ]')
    revised = build_snapshot(tmp_path)
    response.update(revision=revised["revision"], verdict="accept")
    response["issues"][0]["status"] = "resolved"
    ref["quote"] = "[ 1, 2 ]"
    with pytest.raises(ValueError, match="No changed observation"):
        validate_review(revised, response, original, observed=[read_evidence(revised, path)["receipt"]])


def test_located_missing_observation_cannot_be_promoted_to_scientific_failure(tmp_path):
    pair = PAIRS[0]
    snapshot = project(tmp_path, pair, "flawed")
    answer, _ = record(snapshot, pair, "flawed", "new")
    answer["checks"]["experiment"] = {"status": "unassessed", "reason": "Measurements unavailable.", "evidence": []}
    answer["issues"][0]["evidence"] = []
    normalized = validate_review(snapshot, answer)
    assert normalized["submitted_verdict"] == "revise" and normalized["verdict"] == "unassessed"
    assert not normalized["deliverable"]
    issue = normalized["issues"][0]
    assert issue["status"] == "unassessed" and issue["evidence"][0]["quote"] == pair["flawed"]
    answer["issues"][0]["claim"] = "A passage that does not exist in the manuscript."
    with pytest.raises(ValueError, match="evidence"):
        validate_review(snapshot, answer)


@pytest.mark.parametrize(("claim", "conflict"), [
    (r"RMSE from $1.962 \pm 0.088$ to $2.829 \pm 0.105$ (a paired increase of $+8.670 \pm 0.067$, or 44%).", True),
    (r"RMSE from $1.962 \pm 0.088$ to $2.829 \pm 0.105$ (a paired increase of $+0.867 \pm 0.067$, or 44%).", False),
    ("went from 3.2 to 2.0, a decrease of 1.2.", False),
    ("went from 3.2 to 2.0, a decrease of -1.2.", False),
    ("went from 2.0 to 3.2, a decrease of 1.2.", True),
    ("fell from 3.21 to 2.03 (an absolute decrease of 1.2).", False),
    ("rose from 0.50 to 0.75, an increase of 50%.", False),
    ("went from 1 to 2, an increase of 2 fold.", False),
    ("went from 1.0 kg to 2.0 kg, an increase of 1000 grams.", False),
    ("% RMSE from 1.962 to 2.829, a paired increase of 8.670.", False),
])
def test_reported_absolute_changes_respect_scale_direction_and_rounding(claim, conflict):
    from ark.review import reported_change_conflicts
    assert bool(reported_change_conflicts(claim)) is conflict
