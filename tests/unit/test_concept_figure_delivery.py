"""Verify that the image delivered by a generation run is the one it reviewed."""
import base64
import hashlib
import io
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from PIL import Image
import pytest

from ark import nano_banana
from ark.latex.compiler import CompilerMixin


def _image(color, fmt):
    output = io.BytesIO()
    Image.new("RGB", (48, 32), color).save(output, format=fmt)
    return output.getvalue()


def test_selected_paperbanana_revision_published_with_its_own_provenance(tmp_path, monkeypatch):
    compiler = CompilerMixin()
    compiler.code_dir = tmp_path
    compiler.latex_dir = tmp_path / "paper"
    compiler.figures_dir = compiler.latex_dir / "figures"
    compiler.figures_dir.mkdir(parents=True)
    compiler.state_dir = tmp_path / "state"
    compiler.log = compiler.log_step = Mock()
    compiler.config = {"paperbanana_text_model": "gemini-fixture-text",
                       "paperbanana_image_model": "gemini-fixture-image",
                       "nano_banana_critic_rounds": 1}
    compiler.run_agent = Mock(return_value=json.dumps([
        {"name": "method", "caption": "Paired evaluation", "section_context": "One conserved split."}
    ]))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "fixture")
    monkeypatch.setattr(nano_banana, "get_api_key", lambda: "fixture")
    monkeypatch.setattr(nano_banana, "review_concept_image", lambda *args: {
        "status": "passed", "provider": "google", "model": args[-1], "checks": _checks("consistent")})
    # Stub only vendor agents, at their Python boundary; run the real manifest
    # path, model selection, image selection and file installation.
    for role in ["Planner", "Visualizer", "Stylist", "Critic", "Retriever"]:
        constructor = Mock()
        if role == "Retriever":
            constructor.return_value.task_config = {"ref_limit": 200}
        module = SimpleNamespace(**{role + "Agent": constructor})
        monkeypatch.setitem(sys.modules, "agents." + role.lower() + "_agent", module)
    vendor = SimpleNamespace(model_config={"api_keys": {"openrouter_api_key": "unrelated-fixture"}})
    def reinitialize():
        assert "api_keys" not in vendor.model_config
        assert "OPENROUTER_API_KEY" not in os.environ
    vendor.reinitialize_clients = Mock(side_effect=reinitialize)
    monkeypatch.setitem(sys.modules, "utils", SimpleNamespace(generation_utils=vendor))
    monkeypatch.setitem(sys.modules, "utils.config", SimpleNamespace(ExpConfig=SimpleNamespace))
    chosen = "target_diagram_critic_desc0_base64_jpg"
    revised = _image("teal", "JPEG")
    result = {"eval_image_field": chosen, chosen: base64.b64encode(revised).decode(),
              "target_diagram_stylist_desc0_base64_jpg": base64.b64encode(_image("red", "JPEG")).decode()}
    processor = Mock()
    processor.return_value.process_single_query = AsyncMock(return_value=result)
    monkeypatch.setitem(sys.modules, "utils.paperviz_processor", SimpleNamespace(PaperVizProcessor=processor))

    assert compiler._generate_nano_banana_figures() == 1
    exp = processor.call_args.kwargs["exp_config"]
    assert (exp.main_model_name, exp.image_gen_model_name, exp.max_critic_rounds) == (
        "gemini-fixture-text", "gemini-fixture-image", 1)
    path = compiler.figures_dir / "method.png"
    manifest = json.loads((path.parent / "figure_manifest.json").read_text())
    fields = manifest["figures"][path.name]
    assert fields["provider"] == "google" and fields["image_model"] == "gemini-fixture-image"
    assert fields["selected_candidate"] == chosen
    assert fields["payload_sha256"] == hashlib.sha256(revised).hexdigest()
    assert fields["artifact_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    with Image.open(path) as actual, Image.open(io.BytesIO(revised)) as original:
        assert actual.format == "PNG" and actual.tobytes() == original.tobytes()
    assert compiler._generate_nano_banana_figures() == 1
    assert processor.call_count == 1
    assert processor.call_args.kwargs["retriever_agent"].task_config["ref_limit"] == 24
    vendor.reinitialize_clients.assert_called_once()
    assert "api_keys" in vendor.model_config
    path.write_bytes(_image("red", "PNG"))
    assert compiler._generate_nano_banana_figures() == 1
    assert processor.call_count == 2
    compiler.run_agent.assert_called_once()
    assert fields["artifact_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_failed_fallback_cannot_relabel_a_stale_file(tmp_path, monkeypatch):
    from google import genai
    monkeypatch.setattr(genai, "Client", Mock())
    monkeypatch.setattr(nano_banana, "_run_planner", lambda *args: "planned")
    monkeypatch.setattr(nano_banana, "_run_stylist", lambda *args: "styled")
    monkeypatch.setattr(nano_banana, "generate_figure", lambda *args, **kwargs: False)
    path = tmp_path / "method.png"
    before = _image("gray", "PNG")
    path.write_bytes(before)
    receipt = {}
    assert not nano_banana.generate_figure_pipeline("method", "caption", "context", path,
                                                    api_key="fixture", receipt=receipt)
    assert path.read_bytes() == before and not receipt


def test_native_figure_preserves_google_response_and_requested_ratio(tmp_path, monkeypatch):
    from google import genai
    payload = _image("gray", "JPEG")
    response = SimpleNamespace(response_id="fake-id", model_version="response-version",
                               parts=[SimpleNamespace(inline_data=SimpleNamespace(data=payload))])
    call = Mock(return_value=response)
    monkeypatch.setattr(genai, "Client", lambda **kw: SimpleNamespace(models=SimpleNamespace(generate_content=call)))
    receipt = {}
    assert nano_banana.generate_figure("method", tmp_path / "diagram.png", api_key="fixture",
                                       aspect_ratio="21:9", receipt=receipt)
    assert call.call_args.kwargs["config"].image_config.aspect_ratio == "21:9"
    assert receipt["response_id"] == "fake-id" and receipt["model_version"] == "response-version"
    assert receipt["image_model"] == call.call_args.kwargs["model"]


def _checks(status):
    checks = {k: {"status": status, "observed": "Compared the visible paths."}
              for k in nano_banana._SEMANTIC_CHECKS}
    checks["printed_labels"]["transcription"] = ["Input", "Output"]
    checks["result_marks"]["depiction"] = "method"
    return checks


def test_generated_result_plot_overrides_an_older_pass_without_a_paid_repair(tmp_path, monkeypatch):
    compiler = CompilerMixin()
    compiler.config = {"paperbanana_text_model": "vision"}
    compiler.figures_dir = tmp_path
    compiler.log = Mock()
    image = tmp_path / "diagram.png"
    image.write_bytes(_image("blue", "PNG"))
    checks = _checks("consistent")
    checks.pop("result_marks")
    checks["printed_labels"]["transcription"] = ["Empirical performance curves", "Measured accuracy", "Test fold"]
    old = {"version": 2, "model": "vision", "status": "passed", "checks": checks,
           "artifact_sha256": hashlib.sha256(image.read_bytes()).hexdigest(), "design_sha256": "older design"}
    manifest = {"figures": {image.name: {"source":"nano_banana", "semantic_review": old}}}
    for operation in ("review_concept_image", "repair_concept_image", "create_study_panel"):
        monkeypatch.setattr(nano_banana, operation, lambda *a: pytest.fail("replaying named results charged the owner"))
    for _ in range(2):
        assert not compiler._check_final_concept_image({"name":"diagram"}, image, manifest, "unused", "16:9")
    assert nano_banana.semantic_verdict(checks) == "contradiction"
    blocked = manifest["figures"][image.name]["semantic_review"]
    assert blocked["status"] == "contradiction" and "Measured accuracy" in blocked["checks"]["result_marks"]["observed"]
    assert hashlib.sha256(image.read_bytes()).hexdigest() == old["artifact_sha256"]


def test_conceptual_marks_and_measurement_stages_are_valid_method_illustrations():
    checks = _checks("consistent")
    checks["printed_labels"]["transcription"] = ["Neighborhood projection", "Experimental seed", "Measure response",
                                                "Evaluate measured results", "Write data-derived plot"]
    checks["result_marks"].update(depiction="method", observed="Example point geometry and a measurement step.")
    assert nano_banana.semantic_verdict(checks) == "passed"
    checks["result_marks"].pop("depiction")
    assert nano_banana.semantic_verdict(checks) == "unavailable"
    checks["result_marks"].update(depiction="experimental_results", observed="Bars labeled as observed cohort outcomes.")
    assert nano_banana.semantic_verdict(checks) == "contradiction"


def test_ocr_preserves_literal_quotes_and_corroborates_rotated_response_axis(tmp_path, monkeypatch):
    from types import SimpleNamespace
    tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
           '5\t1\t1\t1\t1\t1\t50\t75\t21\t8\t91\t"Test\n'
           '5\t1\t1\t1\t1\t2\t72\t75\t21\t8\t96\tfold\n')
    monkeypatch.setattr(nano_banana.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=tsv))
    trial = nano_banana._tesseract_lines("ocr", tmp_path, 11)[0]
    assert [word["text"] for word in trial] == ['"Test', 'fold']
    def line(words, x, y):
        return [{"text":w, "confidence":95, "box":[x+24*i,y,23,8]} for i,w in enumerate(words.split())]
    heading = line("Observed curves", 50, 12)
    image = tmp_path / "method.png"
    Image.new("RGB", (160, 110), "white").save(image)
    rotate = Mock(side_effect=lambda *a: [line("Measured errors", 0, 0)])
    monkeypatch.setattr(nano_banana, "_tesseract_lines", rotate)
    assert nano_banana._ocr_chart_labels(image, "ocr", [[heading, trial], [heading, trial]]) == [
        "observed curves", "measured errors", "test fold"]
    assert rotate.call_count == 2
    assert not nano_banana._ocr_chart_labels(image, "ocr", [[heading, trial], [trial]])


def test_vision_review_requires_observed_contradiction_on_actual_pixels(tmp_path, monkeypatch):
    from google import genai
    path = tmp_path / "diagram.png"
    path.write_bytes(_image("green", "PNG"))
    calls = []
    checks = _checks("consistent")
    checks["arrows"].update(status="contradiction", observed="Arrow from A enters B.",
                            expected="Output goes to C.", correction="Move the head from B to C.")
    checks["data_separation"].update(status="unclear")
    def respond(client, model, prompt, pixels, receipt):
        calls.append((model, prompt, pixels))
        return json.dumps({"checks": checks})
    monkeypatch.setattr(genai, "Client", Mock())
    monkeypatch.setattr(nano_banana, "_call_gemini_text", respond)
    def review():
        return nano_banana.review_concept_image(path, "Held-out design", "google", "fake", "vision")
    assert review()["status"] == "contradiction"
    assert calls[0][2] == path.read_bytes() and "Held-out design" in calls[0][1]
    del checks["arrows"]["correction"]
    assert review()["status"] == "unavailable"
    checks["arrows"].update(status="consistent")
    assert review()["status"] == "unclear"
    checks["data_separation"].update(status="consistent")
    assert review()["status"] == "passed"


@pytest.mark.parametrize("second", ["passed", "contradiction", "unavailable"])
def test_existing_final_image_is_checked_repaired_once_then_rechecked_on_new_bytes(tmp_path, monkeypatch, second):
    from ark.figure_manifest import load_manifest, save_manifest
    compiler = CompilerMixin()
    compiler.code_dir = tmp_path
    compiler.latex_dir = tmp_path / "paper"
    compiler.figures_dir = compiler.latex_dir / "figures"
    compiler.figures_dir.mkdir(parents=True)
    compiler.state_dir = tmp_path / "state"
    compiler.state_dir.mkdir()
    (compiler.state_dir / "project_context.md").write_text("## Experiment Guidance\nControl A and held-out cohort B")
    compiler.config = {"paperbanana_text_model": "vision-fixture"}
    compiler.log = compiler.log_step = Mock()
    compiler._try_paperbanana = compiler.run_agent = Mock(side_effect=AssertionError("Repeated full generation"))
    monkeypatch.setattr(nano_banana, "get_api_key", lambda: "dummy")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    path = compiler.figures_dir / "overview.png"
    baseline, revised = _image("red", "PNG"), _image("green", "PNG")
    path.write_bytes(baseline)
    orig = hashlib.sha256(baseline).hexdigest()
    save_manifest(compiler.figures_dir, {"figures": {path.name: {
        "source": "paperbanana", "provider": "google", "image_model": "fixture-image", "artifact_sha256": orig}}})
    (compiler.figures_dir / "concept_figures.json").write_text(json.dumps([
        {"name": "overview", "section_context": "B is isolated from A."}]))
    seen = []
    def inspect(p, design, provider, key, model):
        assert "held-out cohort B" in design and "B is isolated from A." in design
        seen.append(p.read_bytes())
        return {"status": "contradiction" if len(seen) == 1 else second, "model": model,
                "provider": provider, "checks": _checks("consistent")}
    def redraw(p, design, review, provider, key, model, aspect_ratio, receipt):
        assert p.read_bytes() == baseline and model == "fixture-image"
        nano_banana._save_generated_image(revised, p, receipt, provider=provider, image_model=model)
        return True
    monkeypatch.setattr(nano_banana, "review_concept_image", inspect)
    monkeypatch.setattr(nano_banana, "create_study_panel", Mock(return_value=None))
    repair = Mock(side_effect=redraw)
    monkeypatch.setattr(nano_banana, "repair_concept_image", repair)
    for _ in range(2):
        assert compiler._generate_nano_banana_figures() == int(second == "passed")
    assert seen == [baseline, revised]
    repair.assert_called_once()
    receipt = load_manifest(compiler.figures_dir)["figures"][path.name]["semantic_review"]
    assert receipt["status"] == second and receipt["repair_attempted"]
    assert receipt["initial_review"]["artifact_sha256"] == orig
    assert receipt["artifact_sha256"] == hashlib.sha256(revised).hexdigest()


def test_study_panel_preserves_original_flash_payload_and_typeset_content(tmp_path, monkeypatch):
    from google import genai
    monkeypatch.setattr(genai, "Client", Mock())
    plan = {"subject": "A single glass sample", "groups": [
        {"label": "Control", "description": "Sample without additive.", "evidence": "without additive"},
        {"label": "Treatment", "description": "Sample with additive.", "evidence": "with additive"}],
        "caption": "Compare the same substrate with and without additive."}
    monkeypatch.setattr(nano_banana, "_call_gemini_text", lambda *a: json.dumps(plan))
    payload = _image("blue", "JPEG")
    def draw(prompt, original, key, model, *, aspect_ratio, receipt):
        assert model == nano_banana.IMAGE_MODELS["flash"] and aspect_ratio == "1:1"
        assert "Sample without additive" not in prompt
        nano_banana._save_generated_image(payload, original, receipt, provider="google", image_model=model)
        return True
    draw_once = Mock(side_effect=draw)
    monkeypatch.setattr(nano_banana, "generate_figure", draw_once)
    path = tmp_path / "study.png"
    receipt = nano_banana.create_study_panel(path, "Compare with additive and without additive.",
                                              "google", "key", "vision", "21:9")
    assert receipt["representation"] == "subject_with_comparison_panel"
    assert receipt["panel_plan"] == plan
    assert (tmp_path / receipt["illustration_file"]).read_bytes() == payload
    assert receipt["payload_sha256"] == hashlib.sha256(payload).hexdigest()
    assert receipt["artifact_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert receipt["artifact_sha256"] != receipt["payload_sha256"]
    with Image.open(path) as image:
        assert image.format == "PNG" and image.width == 1600
    plan["groups"][0]["evidence"] = "unsupported additions"
    assert nano_banana.create_study_panel(path, "Only a sample", "google", "key", "vision", "21:9") is None
    draw_once.assert_called_once()


@pytest.mark.parametrize("final_status", ["passed", "contradiction"])
def test_upgrading_rubric_keeps_prior_edit_budget_and_reviews_entire_study_panel(tmp_path, monkeypatch, final_status):
    from ark.figure_manifest import save_manifest
    compiler = CompilerMixin()
    compiler.config = {"paperbanana_text_model": "vision"}
    compiler.figures_dir = tmp_path
    compiler.log = Mock()
    path = tmp_path / "f.png"
    path.write_bytes(_image("blue", "PNG"))
    fig = {"name": "f", "section_context": "Compare two groups."}
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    design = hashlib.sha256(nano_banana.concept_design(fig, None).encode()).hexdigest()
    entry = {"source": "paperbanana", "provider": "google", "selected_candidate": "semantic_edit",
             "artifact_sha256": original, "semantic_review": {
                 "version": 1, "status": "passed", "model": "vision", "artifact_sha256": original,
                 "design_sha256": design, "repair_attempted": True, "initial_review": {"status": "contradiction"}}}
    manifest = {"figures": {path.name: entry}}
    save_manifest(tmp_path, manifest)
    images = []
    def inspect(p, goal, provider, key, model):
        images.append(p.read_bytes())
        if len(images) == 2:
            assert "subject-only illustration" in goal and "Compare two groups." in goal
        return {"status": "contradiction" if len(images) == 1 else final_status, "model": model,
                "provider": provider, "checks": _checks("consistent")}
    panel_pixels = _image("green", "PNG")
    def make(p, *args):
        p.write_bytes(panel_pixels)
        return {"artifact_sha256": hashlib.sha256(panel_pixels).hexdigest(),
                "representation": "subject_with_comparison_panel", "selected_candidate": "typeset_study_panel"}
    monkeypatch.setattr(nano_banana, "review_concept_image", inspect)
    monkeypatch.setattr(nano_banana, "repair_concept_image", Mock(side_effect=AssertionError("Second edit")))
    panel = Mock(side_effect=make)
    monkeypatch.setattr(nano_banana, "create_study_panel", panel)
    for _ in range(2):
        assert compiler._check_final_concept_image(fig, path, manifest, "fixture", "21:9") == (final_status == "passed")
    assert images == [_image("blue", "PNG"), panel_pixels] and panel.call_count == 1
    review = entry["semantic_review"]
    assert review["panel_attempted"] and review["repair_attempted"]
    assert review["edited_review"]["status"] == "contradiction"


def test_delivery_separates_missing_visual_evidence_from_observed_error(tmp_path):
    import fitz
    from ark.delivery_contract import _concept_figure_in_pdf
    from ark.figure_manifest import save_manifest

    figs = tmp_path / "figures"
    figs.mkdir()
    state = tmp_path / "state"
    spec = {"name": "method", "section_context": "Group A and group B."}
    (figs / "concept_figures.json").write_text(json.dumps([spec]))
    art = figs / "method.png"
    art.write_bytes(_image("green", "PNG"))
    doc = fitz.open()
    doc.new_page().insert_image(fitz.Rect(30, 50, 150, 140), filename=str(art))
    pdf = tmp_path / "main.pdf"
    doc.save(pdf)
    doc.close()
    digest = hashlib.sha256(art.read_bytes()).hexdigest()
    design = hashlib.sha256(nano_banana.concept_design(spec, state).encode()).hexdigest()
    entry = {"source": "nano_banana", "artifact_sha256": digest}
    manifest = {"figures": {"method.png": entry}}
    def check():
        save_manifest(figs, manifest)
        return _concept_figure_in_pdf(pdf, figs, semantic_state=state)
    assert check().status == "skipped"
    review = {"version": nano_banana.SEMANTIC_REVIEW_VERSION, "status": "unavailable",
              "artifact_sha256": digest, "design_sha256": design}
    entry["semantic_review"] = review
    assert "unavailable" in check().detail
    review["status"] = "contradiction"
    assert check().status == "violated"
    review["status"] = "passed"
    assert check().status == "skipped"
    review["checks"] = _checks("consistent")
    assert check().status == "pass"
    (figs / "concept_figures.json").write_text(json.dumps([{**spec, "section_context": "Changed comparison."}]))
    assert check().status == "skipped"


@pytest.mark.parametrize(("confidence", "offset", "mode_two_word", "expected"), [
    (92.0, 1, "Secend", True), (89.9, 1, "Secend", False),
    (92.0, 12, "Secend", False), (92.0, 0, "Second", False),
])
def test_ocr_conflict_needs_two_high_confidence_colocated_word_sequences(
        tmp_path, monkeypatch, confidence, offset, mode_two_word, expected):
    path = tmp_path / "study.png"
    path.write_bytes(_image("red", "PNG"))
    checks = _checks("consistent")
    checks["printed_labels"]["transcription"] = ["Second Stage", "T*", "Hidden"]
    review = {"status": "passed", "checks": checks}
    monkeypatch.setattr(nano_banana.shutil, "which", lambda _: "tesseract")
    def read(binary, path, mode):
        return [[{"text": "Secend" if mode == 6 else mode_two_word, "confidence": confidence,
                  "box": [20 + (0 if mode == 6 else offset), 12, 67, 18]},
                 {"text": "Stage", "confidence": 99, "box": [90, 12, 62, 18]}]]
    read = Mock(side_effect=read)
    monkeypatch.setattr(nano_banana, "_tesseract_lines", read)
    result = nano_banana.verify_printed_labels(path, review, {}, "16:9")
    assert (result["status"] == "contradiction") == expected
    assert result["label_verification"]["coverage"] == "two_ocr_layouts"
    assert nano_banana.verify_printed_labels(path, result, {}, "16:9") is result
    assert read.call_count == 2
    if expected:
        [evidence] = result["label_verification"]["conflicts"]
        assert evidence["text"] == "Secend" and evidence["model_word"] == "Second"
        assert evidence["confidence"] == evidence["confirming_confidence"] == confidence
    # An installed executable with a failed language load must never claim coverage.
    monkeypatch.setattr(nano_banana, "_tesseract_lines", Mock(side_effect=OSError("missing eng data")))
    no_ocr = nano_banana.verify_printed_labels(path, review, {}, "16:9")
    assert no_ocr["status"] == "passed" and no_ocr["label_verification"]["coverage"] == "unavailable"


def test_ocr_ignores_ambiguous_phrases_symbols_and_exact_alternate_occurrences():
    def line(*words):
        return [{"text": w, "confidence": 95, "box": [12, 8, 34, 12]} for w in words]
    assert not nano_banana._label_differences(["Second Stage"], [line("Secend", "Stage"), line("Secend", "Stage")])
    assert not nano_banana._label_differences(["Second Stage"], [line("Secend", "Stage"), line("Second", "Stage")])
    assert not nano_banana._label_differences(["Second", "PCA1 covariance"], [line("Secend", "Stage"), line("PCA1", "covarience")])


def test_hybrid_art_gets_a_local_disclosure_without_changing_other_captions(tmp_path):
    from ark.figure_manifest import save_manifest
    compiler = CompilerMixin()
    compiler.figures_dir = tmp_path / "figures"
    compiler.figures_dir.mkdir()
    compiler.config = {"language": "en"}
    save_manifest(compiler.figures_dir, {"figures": {"overview.png": {"representation": "subject_with_comparison_panel"}}})
    doc = tmp_path / "main.tex"
    doc.write_text(r"""\begin{figure*}\includegraphics[width=\linewidth]{figures/overview.png}
\caption[Short]{Comparison of $k$ settings.}\end{figure*}
\begin{figure}\includegraphics{other/overview.png}\caption{Real measurements.}\end{figure}
""")
    compiler._disclose_study_panel_art(doc)
    marked = doc.read_text()
    assert marked.count("conceptual illustration") == 1
    assert "\\caption{Real measurements.}" in marked
    assert "Comparison of $k$ settings." in marked
    compiler._disclose_study_panel_art(doc)
    assert doc.read_text() == marked


def test_mechanism_receipt_binds_rendered_steps_to_approved_fields_and_original_art(tmp_path, monkeypatch):
    import fitz
    from ark.delivery_contract import _concept_figure_in_pdf
    from ark.figure_manifest import save_manifest

    state = tmp_path / "state"
    state.mkdir()
    approved = {"question": "How does layer curing change stiffness?", "baseline": "Untreated film",
                "comparison": "Cure each layer for 12 s", "controls": ["Use a 3 mm film"], "metric": "Elastic modulus (MPa)"}
    (state / "native_project.json").write_text(json.dumps({"plan": approved}))
    spec = {"name": "process", "representation": "schematic", "mechanism_steps": ["controls[0]", "comparison", "metric"]}
    plan = nano_banana.mechanism_panel_plan(approved, spec["mechanism_steps"])
    assert [g["description"] for g in plan["groups"]] == [approved["controls"][0], approved["comparison"], approved["metric"]]
    assert [g["source_fields"] for g in plan["groups"]] == [[k] for k in spec["mechanism_steps"]]
    payload = _image("purple", "JPEG")

    def draw(prompt, output, key, model, *, aspect_ratio, receipt):
        assert output.suffix == ".image" and approved["question"] in prompt
        assert "Do not encode quantities" in prompt and "12 s" not in prompt and "3 mm" not in prompt
        nano_banana._save_generated_image(payload, output, receipt, provider="google", image_model=model,
                                          payload_source="google_inline_data", response_id="unit-test")
        return True

    draw_once = Mock(side_effect=draw)
    monkeypatch.setattr(nano_banana, "generate_figure", draw_once)
    fig = tmp_path / "figures" / "process.png"
    entry = nano_banana.create_study_panel(fig, "approved test protocol", "google", "fixture", "vision", "16:9",
                                           approved_plan=approved, mechanism_plan=plan)
    assert draw_once.call_count == 1
    raw = fig.parent / entry["illustration_file"]
    assert raw.read_bytes() == payload and entry["payload_sha256"] != entry["artifact_sha256"]
    assert nano_banana.reproduced_panel_labels(fig, entry, "16:9") == [g[k] for g in plan["groups"] for k in ("label", "description")]
    entry["source"] = "nano_banana"
    (fig.parent / "concept_figures.json").write_text(json.dumps([spec]))
    pdf = tmp_path / "main.pdf"
    with fitz.open() as doc:
        doc.new_page().insert_image(fitz.Rect(20, 40, 550, 360), filename=str(fig))
        doc.save(pdf)
    def check():
        save_manifest(fig.parent, {"figures": {fig.name: entry}})
        return _concept_figure_in_pdf(pdf, fig.parent, semantic_state=state).status
    assert check() == "skipped"
    entry["semantic_review"] = {"version": nano_banana.SEMANTIC_REVIEW_VERSION, "status": "contradiction",
                                "artifact_sha256": entry["artifact_sha256"], "checks": _checks("consistent"),
                                "design_sha256": hashlib.sha256(nano_banana.concept_design(spec, state).encode()).hexdigest()}
    assert check() == "violated"
    entry["semantic_review"]["status"] = "passed"
    assert check() == "pass"
    plan["groups"][1]["source_fields"] = ["baseline"]
    assert check() == "skipped"
    plan["groups"][1]["source_fields"] = ["comparison"]
    plan["groups"][1]["description"] = "Cure each layer for 120 s"
    assert check() == "skipped" and not nano_banana.reproduced_panel_labels(fig, entry, "16:9")
    plan["groups"][1]["description"] = approved["comparison"]
    raw.write_bytes(_image("orange", "JPEG"))
    assert check() == "skipped" and not nano_banana.reproduced_panel_labels(fig, entry, "16:9")
