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
