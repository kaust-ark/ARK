"""Corrupt independently measurable dimensions of the fresh manuscript benchmark."""
import json

import fitz
from PIL import Image
import pytest

from scripts import eval_manuscript as benchmark


def test_replay_refuses_swapped_conditions_or_seeds():
    oracle = {0: {"rmse_all": 2.1, "rmse_signal": 1.7}, 1: {"rmse_all": 2.2, "rmse_signal": 1.8}}
    rows = [{"split": i, "all": v["rmse_all"], "signal": v["rmse_signal"]} for i, v in oracle.items()]
    mapping = {"seed": "split", "rmse_all": "all", "rmse_signal": "signal"}
    assert benchmark.replay(rows, mapping, oracle) == oracle
    with pytest.raises(ValueError, match="rmse_all"):
        benchmark.replay(rows, dict(mapping, rmse_all="signal"), oracle)
    rows[1]["split"] = 0
    with pytest.raises(ValueError, match="duplicate"):
        benchmark.replay(rows, mapping, oracle)


def test_shuffled_pdf_rows_do_not_match_split_values():
    with fitz.open() as doc:
        pg = doc.new_page()
        pg.insert_text((72, 100), "0   2417.4   542.5")
        pg.insert_text((72, 124), "1   3214.7   675.4")
        rows = {0: {"nll_raw": 2417.4, "nll_cal": 542.5}, 1: {"nll_raw": 3214.7, "nll_cal": 675.4}}
        assert set(benchmark.displayed_metrics(doc, "digits", rows)) == {0, 1}
        with pytest.raises(ValueError, match="seeds"):
            benchmark.displayed_metrics(doc, "digits", {1: rows[0], 0: rows[1]})


def test_plot_and_method_output_must_match_actual_body_pixels_and_vectors(tmp_path):
    figures = tmp_path / "figures"
    figures.mkdir()
    art = figures / "method.png"
    Image.new("RGB", (80, 60), "orange").save(art)
    plot = figures / "data.pdf"
    with fitz.open() as vector:
        p = vector.new_page(width=150, height=100)
        p.draw_line((20, 40), (110, 70))
        p.draw_circle((60, 50), 5)
        vector.save(plot)
    manifest = {"figures": {"method.png": {"source": "nano_banana", "provider": "google",
                                            "image_model": "gemini-image", "payload_sha256": benchmark.sha256(art),
                                            "artifact_sha256": benchmark.sha256(art)},
                             "data.pdf": {"source": "matplotlib"}}}
    (figures / "figure_manifest.json").write_text(json.dumps(manifest))
    with fitz.open() as doc, fitz.open(plot) as vector:
        for _ in range(6):
            doc.new_page()
        doc[0].show_pdf_page(fitz.Rect(80, 120, 230, 220), vector, 0)
        doc[5].insert_image(fitz.Rect(72, 120, 122, 180), filename=str(art))
        with pytest.raises(ValueError, match="Missing matched"):
            benchmark.check_graphics(doc, tmp_path)
        doc[0].insert_image(fitz.Rect(72, 220, 122, 280), filename=str(art))
        checks = benchmark.check_graphics(doc, tmp_path)
        assert checks["method_art"] == {"method.png": 1}
        assert checks["measured_plot_assets"] == {"data.pdf": 1}
        regenerated, changed = tmp_path / "independent", tmp_path / "perturbed"
        regenerated.mkdir()
        changed.mkdir()
        (regenerated / plot.name).write_bytes(plot.read_bytes())
        (changed / plot.name).write_bytes(plot.read_bytes())
        with pytest.raises(ValueError, match="data plot"):
            benchmark.check_graphics(doc, tmp_path, (regenerated, changed))
        with fitz.open() as different:
            different.new_page(width=150, height=100).draw_circle((70, 45), 30)
            different.save(changed / plot.name)
        del manifest["figures"]["data.pdf"]
        (figures / "figure_manifest.json").write_text(json.dumps(manifest))
        measured = benchmark.check_graphics(doc, tmp_path, (regenerated, changed))
        assert "data.pdf" in measured["reproduced_with_changed_input_control"]
        Image.new("RGB", (80, 60), "blue").save(art)
        with pytest.raises(ValueError, match="Missing matched"):
            benchmark.check_graphics(doc, tmp_path)


def test_google_report_must_match_receipt_and_citations_in_body(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    report = state / "deep_research.md"
    report.write_text("**Interaction ID**: e123\nA survey of probability calibration. DOI 10.1234/tests.")
    receipt = {"provider": "google", "agent": "deep-research-preview-test", "interaction_id": "e123", "status": "completed",
               "report_sha256": benchmark.sha256(report)}
    (state / "deep_research_receipt.json").write_text(json.dumps(receipt))
    (tmp_path / "references.bib").write_text(
        "@article{a,title={A Survey of Probability Calibration},author={Smith}}\n"
        "@article{b,title={Other Paper},doi={10.1234/tests},author={Lee}}")
    raw = tmp_path / "authenticated_interaction.json"
    response = {"id": "e123", "status": "completed", "agent": receipt["agent"],
                "steps": [{"type": "model_output", "content": [{"type": "text", "text": "A survey of probability calibration."}]}]}
    raw.write_text(json.dumps(response))

    class Page:
        number = 0
        rect = fitz.Rect(0, 0, 600, 800)

        def get_textbox(self, rect):
            return "Calibration changes the scale of reported probabilities under this transform."

        def __init__(self, destinations):
            self.destinations = destinations

        def get_links(self):
            return [{"nameddest": "cite." + key, "page": 4, "from": fitz.Rect(55, 155, 110, 165)} for key in self.destinations]

    class Document(list):
        def resolve_names(self):
            return {"appendix.A": {"page": 6}}

    # The second cited paper is on the continuation page of the bibliography.
    class SecondPage(Page):
        def get_links(self):
            return [dict(link, page=5) for link in super().get_links()]

    doc = Document([Page("a"), SecondPage("b"), Page(""), Page(""), Page(""), Page(""), Page("")])
    assert len(benchmark.check_sources(doc, tmp_path, state, raw)["research_sources_used"]) == 2
    with pytest.raises(ValueError, match="Fewer than two"):
        benchmark.check_sources(Document([Page("a")] * 4 + [Page("b")] * 3), tmp_path, state, raw)
    bib = tmp_path / "references.bib"
    verified = bib.read_text()
    bib.write_text(verified.replace("@article{b", "% [NEEDS-CHECK: citation not verified]\n@article{b"))
    with pytest.raises(ValueError, match="unresolved or unattributed"):
        benchmark.check_sources(doc, tmp_path, state, raw)
    bib.write_text(verified)
    raw.write_text(json.dumps(dict(response, id="other-interaction")))
    with pytest.raises(ValueError, match="authenticated provider"):
        benchmark.check_sources(doc, tmp_path, state, raw)
    response["steps"][0]["content"].append({"type": "text", "text": "This second genuine block was lost."})
    raw.write_text(json.dumps(response))
    with pytest.raises(ValueError, match="lost text"):
        benchmark.check_sources(doc, tmp_path, state, raw)
    report.write_text(report.read_text() + "replacement")
    with pytest.raises(ValueError, match="digest"):
        benchmark.check_sources(doc, tmp_path, state, raw)


def test_scheduler_rejects_unrelated_success_and_empty_stdout(tmp_path):
    stdout = tmp_path / "slurm-654.out"
    accounting = tmp_path / "sacct.psv"
    stdout.write_text("experiment completed\n")
    accounting.write_text(" 654|COMPLETED|0:0\n")
    assert benchmark.scheduler(tmp_path, "654", accounting)["job_id"] == "654"
    accounting.write_text("6541|COMPLETED|0:0\n")
    with pytest.raises(ValueError, match="accounting"):
        benchmark.scheduler(tmp_path, "654", accounting)
    accounting.write_text("654|COMPLETED|0:0\n")
    stdout.write_text("")
    with pytest.raises(ValueError, match="worker output"):
        benchmark.scheduler(tmp_path, "654", accounting)


@pytest.mark.parametrize("case", ["digits", "friedman"])
def test_fresh_case_starts_with_brief_and_template_only(tmp_path, monkeypatch, case):
    import sys
    import yaml
    from scripts import e2e_project

    root = tmp_path / "project"
    backend = tmp_path / "slurm.yaml"
    backend.write_text("type: slurm\n")
    monkeypatch.setattr(sys, "argv", ["e2e_project.py", "--dir", str(root), "--model", "gemini/test",
                                     "--manuscript-case", case, "--slurm-config", str(backend)])
    e2e_project.main()
    config = yaml.safe_load((root / "config.yaml").read_text())
    state = root / "auto_research/state"
    assert sorted(p.name for p in state.iterdir()) == ["idea.md"]
    assert (state / "idea.md").read_text() == (e2e_project.REPO / f"test_fixtures/manuscript/{case}.md").read_text()
    assert (root / "paper/main.tex").read_bytes() == (e2e_project.REPO / "venue_templates/icml/main.tex").read_bytes()
    assert not list((root / "paper/figures").iterdir())
    assert not (root / "results").exists() and not (root / "paper/main.pdf").exists()
    assert (config["skip_deep_research"], config["figure_generation"]) == (False, "nano_banana")
    assert config["experiment_compute_backend"]["type"] == "slurm"
    with pytest.raises(SystemExit, match="fresh directory"):
        e2e_project.main()


def test_table_check_uses_printed_precision_and_direction():
    assert benchmark.printed_value_matches("2.70", 2.7016)
    assert not benchmark.printed_value_matches("2.71", 2.7016)
    assert not benchmark.printed_value_matches("-2.70", 2.7016)
    assert benchmark.printed_value_matches("3.26e5", 326062.283)
    assert not benchmark.printed_value_matches("3e5", 326062.283)


def test_fourth_page_full_width_chart_counts_at_its_rendered_location():
    with fitz.open() as doc, fitz.open() as vector:
        vector.new_page(width=440, height=100).draw_rect((2, 2, 438, 98))
        for n in range(6):
            page = doc.new_page(width=600, height=800)
            if n < 4:
                page.insert_text((70, 180), "We report further measurements in Appendix A.")
            elif n == 4:
                page.insert_text((70, 120), "References")
            else:
                page.insert_textbox(fitz.Rect(70, 120, 540, 400), "Reproducible experimental protocol. " * 14)
        doc.xref_set_key(doc.pdf_catalog(), "Dests", f"<< /appendix.A [{doc.page_xref(5)} 0 R /Fit] >>")
        doc[3].show_pdf_page(fitz.Rect(80, 55, 520, 155), vector, 0)
        with pytest.raises(ValueError, match="column fill"):
            benchmark.layout(doc)
        doc[3].show_pdf_page(fitz.Rect(80, 580, 520, 680), vector, 0)
        assert .9 < benchmark.layout(doc)["last_page_fill"] < .95


def test_semantic_review_binds_final_pixels_and_study_design(tmp_path):
    import hashlib
    from ark.nano_banana import SEMANTIC_REVIEW_VERSION, concept_design

    figures = tmp_path / "paper/figures"
    figures.mkdir(parents=True)
    spec = {"name": "method", "description": "Temperature selection uses held-out calibration data."}
    art = figures / "method.png"
    art.write_bytes(b"selected method illustration")
    (figures / "concept_figures.json").write_text(json.dumps([spec]))
    review = {"model": "vision", "version": SEMANTIC_REVIEW_VERSION, "status": "passed", "artifact_sha256": benchmark.sha256(art),
              "design_sha256": hashlib.sha256(concept_design(spec, None).encode()).hexdigest(),
              "checks": {"comparison_groups": "pass"}}
    path = figures / "figure_manifest.json"
    path.write_text(json.dumps({"figures": {art.name: {"semantic_review": review}}}))
    embedded = {"method_art": {art.name: 2}}
    assert benchmark.check_diagram_review(figures.parent, embedded)[art.name]["model"] == "vision"
    spec["description"] = "Modified experimental design."
    (figures / "concept_figures.json").write_text(json.dumps([spec]))
    with pytest.raises(ValueError, match="study design"):
        benchmark.check_diagram_review(figures.parent, embedded)
    review["status"] = "contradiction"
    path.write_text(json.dumps({"figures": {art.name: {"semantic_review": review}}}))
    with pytest.raises(ValueError, match="contradiction"):
        benchmark.check_diagram_review(figures.parent, embedded)


def test_composed_panel_needs_native_bytes_and_a_matching_authenticated_receipt(tmp_path):
    import hashlib
    from ark.nano_banana import SEMANTIC_REVIEW_VERSION, _compose_study_panel, concept_design

    paper = tmp_path / "paper"
    figures = paper / "figures"
    figures.mkdir(parents=True)
    native = figures / "native.jpg"
    Image.new("RGB", (80, 50), "orange").save(native)
    fig = figures / "method.png"
    groups = [{"label": s, "description": f"Measure the {s.lower()} arm.", "evidence": s}
              for s in ("Baseline", "Calibrated")]
    spec = {"name": "method", "description": "Baseline and Calibrated arms share a test split."}
    _compose_study_panel(native, groups, fig, False)
    (figures / "concept_figures.json").write_text(json.dumps([spec]))
    review = {"model": "vision", "version": SEMANTIC_REVIEW_VERSION, "status": "passed",
              "artifact_sha256": benchmark.sha256(fig),
              "design_sha256": hashlib.sha256(concept_design(spec, None).encode()).hexdigest()}
    record = {"representation": "subject_with_comparison_panel", "semantic_review": review,
              "illustration_file": native.name, "payload_source": "google_inline_data", "response_id": "r1",
              "model_version": "image-v1", "payload_sha256": benchmark.sha256(native),
              "panel_plan": {"groups": groups, "caption": "Shared test data compare two arms."}}
    (figures / "figure_manifest.json").write_text(json.dumps({"figures": {fig.name: record}}))
    receipt = tmp_path / "google.json"
    response = {"responseId": "r1", "modelVersion": "image-v1", "payloads": [{"sha256": benchmark.sha256(native)}]}
    receipt.write_text(json.dumps(response))
    embedded = {"method_art": {fig.name: 2}}
    assert benchmark.check_diagram_review(paper, embedded, [receipt])[fig.name]["authenticated_native_payload"]
    response["responseId"] = "wrong"
    receipt.write_text(json.dumps(response))
    with pytest.raises(ValueError, match="authenticated image"):
        benchmark.check_diagram_review(paper, embedded, [receipt])
    Image.new("RGB", (80, 50), "purple").save(native)
    with pytest.raises(ValueError, match="unbound native"):
        benchmark.check_diagram_review(paper, embedded, [receipt])


def test_independent_visual_observation_is_bound_to_pdf_and_art(tmp_path):
    figures = tmp_path / "figures"
    figures.mkdir()
    art = figures / "method.png"
    art.write_bytes(b"checked illustration")
    file = tmp_path / "review.json"
    notes = {"pass": True, "artifact_sha256": benchmark.sha256(art), "observations": ["Both arms are visible."]}
    review = {"pdf_sha256": "pdf1", "diagrams": {art.name: notes}}
    file.write_text(json.dumps(review))
    embedded = {"method_art": {art.name: 1}}
    assert benchmark.visual_review(file, "pdf1", tmp_path, embedded) == review
    with pytest.raises(ValueError, match="another PDF"):
        benchmark.visual_review(file, "pdf2", tmp_path, embedded)
    notes.update({"pass": False, "observations": ["The baseline was removed."]})
    file.write_text(json.dumps(review))
    with pytest.raises(ValueError, match="baseline was removed"):
        benchmark.visual_review(file, "pdf1", tmp_path, embedded)
