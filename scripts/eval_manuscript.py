"""Offline acceptance of a completed, clean-start four-page ICML manuscript.

Prepare with scripts/e2e_project.py --manuscript-case {digits,friedman}. After
the ordinary run, save the scheduler's independent accounting output with
sacct -n -P -X -j JOB -o JobIDRaw,State,ExitCode. Identify the experiment job
from its project log. Save the authenticated Google Interactions GET response
for the research ID as JSON outside the worker's project. Run, for example:

  python scripts/eval_manuscript.py digits PROJECT --rows results/by_seed.csv \
    --job-id 123 --sacct /tmp/sacct.psv --google-response /tmp/interaction.json \
    --output /tmp/evaluation.json

For differently named columns, pass --column nll_raw=baseline_log_loss, etc.
JSON arrays (including nested rows with --json-path per_seed) and CSV work.
To verify data dependence, pass --replayed-plots CLEAN_FIGURES PERTURBED_FIGURES,
the outputs of independently running the worker's plotting script on its real
input and on permuted input in disposable copies. Canonical measured fields
appear in reference_rows(). Use the worker's scikit-learn
version for replay. Required tools: PyMuPDF, sklearn, SciPy, TeX Live/latexmk.
Exit 0 means the recorded checks passed. Matched sources are evidence that
research reached the paper; the citations' scientific interpretation still
calls for reading the sources. No remote request or model call is made.
"""
import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import fitz

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from ark.citation import parse_bib
from ark.delivery_contract import evaluate
from ark.figure_manifest import AI_SOURCES, load_manifest
from ark.latex.utils import body_page_boundary


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reference_rows(case):
    """Fit independently from the protocol without importing project scripts."""
    import numpy as np
    from scipy.special import logsumexp
    from sklearn.datasets import load_digits, make_friedman1
    from sklearn.metrics import mean_absolute_error, mean_squared_error
    from sklearn.model_selection import train_test_split
    from sklearn.naive_bayes import GaussianNB
    from sklearn.neighbors import KNeighborsRegressor

    rows = {}
    if case == "digits":
        x, y = load_digits(return_X_y=True)
        grid = (1, 2, 4, 8, 16, 32, 64)
        for seed in range(5):
            pool, test = train_test_split(np.arange(len(y)), test_size=.2, random_state=seed, stratify=y)
            train, cal = train_test_split(pool, test_size=.25, random_state=seed, stratify=y[pool])
            clf = GaussianNB(var_smoothing=1e-9).fit(x[train], y[train])

            def nll(scores, target, t):
                z = scores / t
                return float(np.mean(logsumexp(z, axis=1) - z[np.arange(len(target)), target]))

            calibration = clf.predict_log_proba(x[cal])
            temperature = min(grid, key=lambda t: nll(calibration, y[cal], t))
            scores = clf.predict_log_proba(x[test])
            pred = scores.argmax(axis=1)
            calibrated = (scores / temperature).argmax(axis=1)
            rows[seed] = dict(temperature=temperature, nll_raw=nll(scores, y[test], 1),
                              nll_cal=nll(scores, y[test], temperature),
                              accuracy=float(np.mean(pred == y[test])),
                              disagreement=float(np.mean(pred != calibrated)))
    else:
        x, y = make_friedman1(n_samples=800, n_features=10, noise=1., random_state=2027)
        for seed in range(5):
            train, test = train_test_split(np.arange(len(y)), test_size=.2, random_state=seed)
            rows[seed] = {}
            for arm, features in (("all", 10), ("signal", 5)):
                clf = KNeighborsRegressor(n_neighbors=5, weights="uniform", metric="euclidean")
                pred = clf.fit(x[train, :features], y[train]).predict(x[test, :features])
                rows[seed][f"rmse_{arm}"] = math.sqrt(mean_squared_error(y[test], pred))
                rows[seed][f"mae_{arm}"] = mean_absolute_error(y[test], pred)
    return rows


def measured_rows(path, json_path):
    if path.suffix.lower() == ".csv":
        with path.open(newline="") as stream:
            return list(csv.DictReader(stream))
    rows = json.loads(path.read_text())
    for key in json_path.split(".") if json_path else ():
        rows = rows[key]
    if not isinstance(rows, list):
        raise ValueError("Expected a JSON list of per-seed records; use --json-path to select it.")
    return rows


def replay(raw, columns, oracle):
    """Bind every observation to its condition and split; a global number match is insufficient."""
    result = {}
    if len(raw) != len(oracle):
        raise ValueError(f"Expected {len(oracle)} split rows, found {len(raw)}")
    for row in raw:
        seed = int(str(row[columns.get("seed", "seed")]))
        if seed not in oracle or seed in result:
            raise ValueError(f"Unexpected or duplicate split {seed}")
        projected = {}
        for field, expected in oracle[seed].items():
            value = row
            for key in columns.get(field, field).split("."):
                value = value[key]
            observed = float(value)
            if not math.isclose(observed, expected, rel_tol=5e-5, abs_tol=5e-5):
                raise ValueError(f"Seed {seed} {field}: measured {observed:g}, replay {expected:g}")
            projected[field] = observed
        result[seed] = projected
    return result


def pdf_rows(doc, clip=None):
    """Keep values bound to the same physical line; allow thousands separators."""
    for page in doc:
        lines = []
        for x, y, _, bottom, token, *_ in sorted(page.get_text("words", clip=clip), key=lambda w: (w[1], w[0])):
            # Math exponents have a slightly higher baseline. Merge them with
            # the base while keeping adjacent table rows separate.
            if not lines or y - lines[-1][0] > max(2., .55 * (bottom - y)):
                lines.append((y, []))
            lines[-1][1].append((x, token))
        for _, words in lines:
            yield page.number + 1, " ".join(w[1] for w in sorted(words))


def printed_value_matches(token, expected):
    """Allow rounding at the precision actually printed for a paired result."""
    token = token.replace(",", "").lower()
    observed = float(token)
    if observed == expected:
        return True
    mantissa, _, exponent = token.partition("e")
    decimals = len(mantissa.partition(".")[2])
    quantum = 10. ** (int(exponent or "0") - decimals)
    return (quantum <= .025 * abs(expected)
            and abs(expected - observed) <= quantum * .500001)


def _check_friedman_tables(doc, rows):
    """Check each rendered per-seed table that labels both feature-space arms."""
    labels = {"seed":"seed"}
    for metric in ("rmse", "mae"):
        for arm, aliases in (("all", ("10", "10f", "full")), ("signal", ("5", "5f", "signal"))):
            for name in aliases:
                labels[f"{metric} ({name})"] = f"{metric}_{arm}"
    errors = []
    observed = {}
    for page in doc:
        for block in page.get_text("dict", flags=0)["blocks"]:
            lines = [("".join(s["text"] for s in line["spans"]).strip().lower(), line["bbox"])
                     for line in block.get("lines", [])]
            for title, box in lines:
                if title != "seed":
                    continue
                header = sorted(((rect[0], labels[key], rect) for key, rect in lines
                                 if key in labels and abs(rect[1] - box[1]) <= 2), key=lambda cell: cell[0])
                columns = [cell[1] for cell in header]
                if len(set(columns)) != len(columns) or not {"rmse_all", "rmse_signal"} <= set(columns):
                    continue
                clip = fitz.Rect(header[0][0] - 2, max(cell[2][3] for cell in header),
                                 header[-1][2][2] + 2, page.rect.height)
                for _, line in pdf_rows([page], clip):
                    cells = line.split()
                    if len(cells) != len(columns) or not all(re.fullmatch(r"\d+(?:\.\d+)?", v) for v in cells):
                        break
                    identifier = re.sub(r"\.0+$", "", cells[columns.index("seed")])
                    if not identifier.isdigit() or int(identifier) not in rows:
                        break
                    observed[int(identifier)] = {"page": page.number + 1, "row": line}
                    row = rows[int(identifier)]
                    for field, printed in zip(columns, cells):
                        if field != "seed" and not printed_value_matches(printed, row[field]):
                            errors.append(f"page {page.number + 1}, seed {identifier}, {field}: printed {printed}, "
                                          f"reference {row[field]:.6g}")
    if errors:
        raise ValueError("Conflicting rendered measurements: " + "; ".join(errors[:12]))
    return observed


def displayed_metrics(doc, case, rows):
    result = _check_friedman_tables(doc, rows) if case == "friedman" else {}
    fields = ("nll_raw", "nll_cal") if case == "digits" else ("rmse_all", "rmse_signal")
    lines = list(pdf_rows(doc))
    for seed, row in rows.items():
        if seed in result:
            continue
        for page, line in lines:
            line = re.sub(r"(\d+(?:\.\d+)?)\s*×\s*10\s*\^?\s*([+−-]?\d{1,2})(?!\d)",
                          lambda m: f"{m[1]}e{m[2].replace('−', '-')}", line)
            tokens = re.findall(r"(?<![\w.])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?(?![\w.])",
                                line.replace("−", "-"))
            if str(seed) not in tokens and not (tokens and re.fullmatch(rf"{seed}\.0+", tokens[0])):
                continue
            if all(any(printed_value_matches(token, row[field]) for token in tokens) for field in fields):
                result[seed] = {"page": page, "row": line}
                break
    if set(result) != set(rows):
        raise ValueError(f"Missing displayed paired {fields} for seeds {sorted(set(rows) - set(result))}")
    return result


def research_provenance(state, provider_response):
    report = state / "deep_research.md"
    receipt = json.loads((state / "deep_research_receipt.json").read_text())
    if not (receipt.get("provider") == "google" and receipt.get("status") == "completed"
            and receipt.get("interaction_id") and receipt["interaction_id"] in report.read_text()
            and receipt.get("report_sha256") == sha256(report)):
        raise ValueError("Native Google completion and report digest do not match")
    if not re.fullmatch(r"deep-research[-\w.]+", str(receipt.get("agent", ""))):
        raise ValueError("Receipt names no Google Deep Research agent")
    response = json.loads(provider_response.read_text())
    if (response.get("id"), response.get("agent"), response.get("status")) != (
        receipt["interaction_id"], receipt["agent"], "completed"
    ):
        raise ValueError("Interaction ID, agent or status differs from the authenticated provider response")
    blocks = response.get("outputs") or [b for step in response.get("steps", []) if step.get("type") == "model_output"
                                         for b in step.get("content", [])]
    # Enriching citation markers with source links may interrupt the exact
    # provider string. Every surrounding passage must survive in the report.
    texts = [b["text"] for b in blocks if b.get("type") == "text" and b.get("text")]
    passages = [part for text in texts for part in re.split(r"\[cite:[^\]]+\]", text) if len(part.strip()) >= 15]
    saved = report.read_text()
    if not passages or not all(part in saved for part in passages):
        raise ValueError("Saved research report lost text from the authenticated provider response")
    return {"interaction": receipt["interaction_id"], "agent": receipt["agent"],
            "provider_response_sha256": sha256(provider_response), "retained_text_blocks": len(texts),
            "grounding": (response.get("usage") or {}).get("grounding_tool_count")}


def check_sources(doc, paper, state, provider_response):
    provenance = research_provenance(state, provider_response)
    appendix_pages = [v["page"] for k, v in doc.resolve_names().items() if k.startswith("appendix.") and "page" in v]
    bibliography_end = min(appendix_pages, default=len(doc))
    body_links = {}
    for page in list(doc)[:4]:
        for link in page.get_links():
            dest = link.get("nameddest", "")
            if dest.startswith("cite.") and 4 <= link.get("page", -1) < bibliography_end:
                key = dest.removeprefix("cite.")
                area = fitz.Rect(link["from"])
                # A narrow strip across the same column captures the actual
                # cited passage, rather than an adjacent bibliography entry.
                middle = page.rect.width / 2
                left, right = (0, middle) if area.x0 < middle else (middle, page.rect.width)
                nearby = page.get_textbox(fitz.Rect(left, area.y0 - 28, right, area.y1 + 22))
                body_links.setdefault(key, {}).setdefault(page.number + 1, " ".join(nearby.split()))
    def normalize(s):
        return " ".join(re.findall(r"[a-z0-9]+", s.casefold()))

    report_text = " " + normalize((state / "deep_research.md").read_text()) + " "
    matches = {}
    unresolved = []
    for entry in parse_bib(str(paper / "references.bib")):
        key, fields = entry["key"], entry["fields"]
        if key in body_links and ("NEEDS-CHECK" in entry["preceding_comments"].upper()
                                  or not (fields.get("author") or fields.get("editor") or fields.get("organization"))):
            unresolved.append(key)
        title, doi = normalize(fields.get("title", "")), normalize(fields.get("doi", ""))
        # Reports may describe a method by name, while the library resolves
        # its full publication title. Match the stable source identifier too.
        arxiv_id = fields.get("eprint", "")
        if not arxiv_id and "arxiv.org/" in fields.get("url", "").lower():
            arxiv_id = fields["url"]
        if not arxiv_id and fields.get("doi", "").lower().startswith("10.48550/arxiv."):
            arxiv_id = fields["doi"].split("/", 1)[1]
        arxiv = re.search(r"\b(\d{4}\.\d{4,5})(?:v\d+)?\b", arxiv_id, re.I)
        if key in body_links and ((len(title.split()) >= 4 and f" {title} " in report_text)
                                  or (doi.startswith("10 ") and f" {doi} " in report_text)
                                  or (arxiv and f" {normalize(arxiv[1])} " in report_text)):
            passages = {page: text for page, text in body_links[key].items() if len(text.split()) >= 8}
            if passages:
                matches[key] = {"title": fields.get("title"), "cited_passages": passages}
    if unresolved:
        raise ValueError(f"Body cites unresolved or unattributed source identities: {', '.join(unresolved)}")
    if len(matches) < 2:
        raise ValueError(f"Fewer than two papers identified in both research report and body citation links: {matches}")
    return {**provenance, "linked_papers": len(body_links),
            "research_sources_used": matches}


def figure_pixels(path):
    if path.suffix.lower() == ".pdf":
        with fitz.open(path) as figure:
            px = figure[0].get_pixmap()
    else:
        px = fitz.Pixmap(str(path))
    return (px.width, px.height, px.digest)


def check_graphics(doc, paper, replayed_plots=None):
    figures = paper / "figures"
    records = load_manifest(figures, infer_existing=False).get("figures", {})
    raster = {}
    vectors = {}
    for page in list(doc)[:4]:
        for image in page.get_image_info(hashes=True):
            raster.setdefault(image["digest"], page.number + 1)
        for xref, *_ in page.get_xobjects():
            vectors.setdefault(hashlib.sha256(doc.xref_stream(xref)).digest(), page.number + 1)
    methods, plots, reproduced = {}, {}, {}
    if replayed_plots:
        # Normal plot scripts need not write a manifest. A rendered match
        # regenerated from the actual data plus a changed-input counterexample
        # gives independent evidence for the figure's measured origin.
        original, perturbed = replayed_plots
        for candidate in original.iterdir():
            name = candidate.name
            delivered, changed = figures / name, perturbed / name
            if (candidate.suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg"}
                    or not delivered.is_file() or not changed.is_file()):
                continue
            expected = figure_pixels(candidate)
            if figure_pixels(delivered) == expected and figure_pixels(changed) != expected:
                reproduced[name] = expected[2].hex()
                records.setdefault(name, {"source": "matplotlib"})
    for name, record in records.items():
        path = figures / name
        if Path(name).name != name or not path.is_file():
            continue
        if record.get("source") in AI_SOURCES:
            if (record.get("provider") != "google" or not record.get("image_model")
                    or not re.fullmatch(r"[a-f0-9]{64}", str(record.get("payload_sha256", "")))
                    or record.get("artifact_sha256") != sha256(path)):
                continue
            px = fitz.Pixmap(str(path))
            if px.alpha:
                px = fitz.Pixmap(px, 0)
            if px.digest in raster:
                methods[name] = raster[px.digest]
        elif record.get("source") == "matplotlib" and (not replayed_plots or name in reproduced):
            if path.suffix.lower() == ".pdf":
                with fitz.open(path) as figure:
                    if not len(figure):
                        continue
                    digest = hashlib.sha256(figure[0].read_contents()).digest()
                if digest in vectors:
                    plots[name] = vectors[digest]
            elif path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
                px = fitz.Pixmap(str(path))
                if px.alpha:
                    px = fitz.Pixmap(px, 0)
                if px.digest in raster:
                    plots[name] = raster[px.digest]
    if not methods or not plots:
        raise ValueError(f"Missing matched Google schematic or data plot in body: {methods}, {plots}")
    return {"method_art": methods, "measured_plot_assets": plots,
            "reproduced_with_changed_input_control": {n: h for n, h in reproduced.items() if n in plots}}


def check_diagram_review(paper, graphics, image_receipts=()):
    from ark.nano_banana import (
        SEMANTIC_REVIEW_VERSION, TYPESET_PANELS, _compose_study_panel, concept_design,
        mechanism_panel_is_grounded, semantic_verdict, verify_printed_labels,
    )

    if not graphics or not graphics["method_art"]:
        raise ValueError("No embedded native method illustration to check")
    figures = paper / "figures"
    records = load_manifest(figures, infer_existing=False)["figures"]
    specs = {row["name"]: row for row in json.loads((figures / "concept_figures.json").read_text())}
    details = {}
    for name in graphics["method_art"]:
        record = records[name]
        review = record.get("semantic_review") or {}
        if review.get("status") != "passed":
            raise ValueError(f"{name}: semantic review {review.get('status', 'missing')}: {review.get('checks', [])}")
        spec = specs[Path(name).stem]
        design = concept_design(spec, paper.parent / "auto_research/state")
        expected = hashlib.sha256(design.encode()).hexdigest()
        fresh = verify_printed_labels(figures / name, {**review, "label_verification": {}}, record,
                                      "4:3" if record.get("placement") == "single_column" else "16:9")
        verdict = semantic_verdict(fresh.get("checks"))
        if fresh.get("status") != "passed" or verdict != "passed":
            marks = (fresh.get("checks") or {}).get("result_marks") or {}
            reason = marks.get("observed", "") if isinstance(marks, dict) else ""
            if verdict == "passed":
                verdict = fresh.get("status")
            raise ValueError(f"{name}: final image check {verdict}. {fresh.get('panel_error') or reason}")
        if (review.get("version") != SEMANTIC_REVIEW_VERSION or review.get("design_sha256") != expected
                or review.get("artifact_sha256") != sha256(figures / name) or not review.get("model")):
            raise ValueError(f"{name}: semantic review does not match the final pixels and study design")
        details[name] = {k: review.get(k) for k in ("model", "artifact_sha256", "design_sha256", "checks")}
        if labels := fresh.get("label_verification"):
            if labels.get("image_sha256") != sha256(figures / name) or labels.get("conflicts"):
                raise ValueError(f"{name}: printed label check is stale or reports a conflict")
            details[name]["printed_labels"] = labels.get("coverage")
        if record.get("representation") in TYPESET_PANELS:
            mechanism = record["representation"] == "subject_with_mechanism_panel"
            raw = (figures / record["illustration_file"]).resolve()
            plan = record["panel_plan"]
            groups = plan["groups"]
            supported = (mechanism_panel_is_grounded(record, spec, paper.parent / "auto_research/state") if mechanism else
                         all(g.get("evidence") and g["evidence"] in design for g in groups))
            if (not raw.is_relative_to(figures.resolve()) or not record.get("response_id")
                    or record.get("payload_source") != "google_inline_data" or sha256(raw) != record["payload_sha256"]
                    or not plan.get("caption") or len(groups) < 2
                    or not supported):
                raise ValueError(f"{name}: unbound native illustration or unsupported protocol panels")
            with tempfile.TemporaryDirectory() as tmp:
                rebuilt = Path(tmp) / "composed.png"
                _compose_study_panel(raw, groups, rebuilt, single_column=record.get("placement") == "single_column",
                                     mechanism=mechanism)
                if figure_pixels(rebuilt) != figure_pixels(figures / name):
                    raise ValueError(f"{name}: saved panel does not reproduce from native pixels and study groups")
            details[name]["caption"] = plan["caption"]
            if mechanism:
                details[name]["mechanism_source_fields"] = {g["label"]: g["source_fields"] for g in groups}
                if not image_receipts or not record.get("model_version"):
                    raise ValueError(f"{name}: no authenticated image response for the original method artwork")
            else:
                details[name]["grounded_comparison_groups"] = [g["label"] for g in groups]
        if record.get("payload_source") == "google_inline_data" and image_receipts:
            valid = any(r.get("responseId") == record["response_id"] and r.get("modelVersion") == record.get("model_version")
                        and any(p.get("sha256") == record["payload_sha256"] for p in r.get("payloads", []))
                        for r in (json.loads(file.read_text()) for file in image_receipts))
            if not valid:
                raise ValueError(f"{name}: no authenticated image response matches the native payload")
            details[name].update(authenticated_native_payload=True, native_response_id=record["response_id"],
                                 native_sha256=record["payload_sha256"])
    return details


def visual_review(report, fingerprint, paper, graphics):
    review = json.loads(report.read_text())
    if review.get("pdf_sha256") != fingerprint:
        raise ValueError("Independent visual review belongs to another PDF")
    if not graphics or not graphics["method_art"]:
        raise ValueError("No matched method illustration for independent visual review")
    for name in graphics["method_art"]:
        verdict = review["diagrams"].get(name, {})
        if verdict.get("artifact_sha256") != sha256(paper / "figures" / name):
            raise ValueError(f"{name}: independent review was on a different illustration")
        if verdict.get("pass") is not True or not verdict.get("observations"):
            raise ValueError(f"{name}: independent visual review: {verdict.get('observations', 'missing')}")
    return review


def layout(doc):
    boundary = body_page_boundary(doc)
    if not boundary or boundary[0] != 4 or any(boundary[1]) or len(doc) < 6:
        raise ValueError(f"Expected four body pages before the endmatter and a separate appendix; got {boundary}")
    # Review-mode line numbers and running titles cannot fill a blank column.
    from ark.latex.compiler import CompilerMixin
    compiler = CompilerMixin()
    for page in list(doc)[:4]:
        text = compiler._body_text_blocks(page)
        width, height = page.rect.width, page.rect.height
        # A PDF form may be translated or scaled at the point of inclusion.
        # MuPDF's paint log already applies those transformations. Exclude
        # page backgrounds, headers and margin decorations from the fill.
        art = [fitz.Rect(b) for kind, b in page.get_bboxlog()
               if kind in {"fill-image", "fill-path", "stroke-path"}
               and width * .06 < b[0] < b[2] < width * .94
               and height * .06 < b[1] < b[3] < height * .95]
        if not (any(re.search(r"[A-Za-z]{3}", b[4]) for b in text)
                or any(b.get_area() > width * height * .1 for b in art)):
            raise ValueError(f"Body page {page.number + 1} has no substantive text or figure")
        if page.number == 3:
            boxes = [fitz.Rect(b[:4]) for b in text] + art
    last = doc[3]
    mid, height = last.rect.width / 2, last.rect.height
    bottom = [max((min(box.y1, height) for box in boxes if box.x0 < right and box.x1 > left), default=0)
              for left, right in [(last.rect.width * .12, mid - 8), (mid + 8, last.rect.width * .88)]]
    # The bundled letter-paper ICML style reserves nine inches for body
    # text. Exclude the surrounding margins from the column utilization.
    column_height = 9 * 72
    top = (height - column_height) / 2
    area = sum(max(0, min(y - top, column_height)) for y in bottom) / (2 * column_height)
    if area < .75:
        raise ValueError(f"Fourth body page has only {area:.0%} average printable column fill")
    names = doc.resolve_names()
    appendix = {v.get("page", -1) + 1 for k, v in names.items() if k.startswith("appendix.")}
    references_page = next((i + 1 for i in range(4, min(appendix, default=len(doc) + 1) - 1)
                            if re.search(r"(?im)^\s*References\s*$", doc[i].get_text())), None)
    if references_page is None:
        raise ValueError("References section missing between the four body pages and appendix")
    linked_appendices = {link.get("nameddest") for page in list(doc)[:4] for link in page.get_links()
                         if str(link.get("nameddest", "")).startswith(("appendix.", "table.", "figure."))
                         and link.get("page", -1) >= 5}
    body_text = " ".join(page.get_text() for page in list(doc)[:4])
    cites_appendix = bool(linked_appendices or re.search(r"\bAppendix\s+[A-Z0-9]\b", body_text, re.I))
    if not any(n >= 6 and sum(len(re.findall(r"[A-Za-z]{3,}", b[4]))
                              for b in compiler._body_text_blocks(doc[n - 1])) >= 15 for n in appendix):
        raise ValueError("No substantial separate appendix in the delivered PDF")
    return {"body_pages": 4, "last_page_fill": area, "references_page": references_page, "appendix_pages": sorted(appendix),
            "body_cross_references_appendix": cites_appendix}


def check_build(paper, delivered_pdf):
    template = REPO / "venue_templates/icml"
    for name in ("icml2026.sty", "icml2026.bst"):
        if sha256(paper / name) != sha256(template / name):
            raise ValueError(f"Official template modified: {name}")
    with tempfile.TemporaryDirectory() as tmp:
        project = paper.resolve().parent
        work = Path(tmp) / "paper"
        work.mkdir()
        inputs = set(paper.resolve().rglob("*"))
        # Legitimate manuscripts also use \input{../...} and graphics
        # elsewhere in the project. The recorder identifies exactly which
        # additional sources were used without copying a conda environment.
        recorder = paper / "main.fls"
        if recorder.exists():
            inputs.update((paper / line[6:]).resolve() for line in recorder.read_text(errors="replace").splitlines()
                          if line.startswith("INPUT "))
        for path in inputs:
            if not path.is_relative_to(project) or not path.is_file() or path == paper.resolve() / "main.pdf":
                continue
            if path.suffix.lower() not in {".tex", ".bib", ".sty", ".bst", ".cls", ".def", ".png", ".jpg", ".jpeg", ".pdf", ".eps", ".csv", ".dat", ".pgf"}:
                continue
            target = work.parent / path.relative_to(project)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        run = subprocess.run(["latexmk", "-norc", "-pdf", "-no-shell-escape", "-halt-on-error",
                              "-interaction=nonstopmode", "main.tex"], cwd=work, capture_output=True, timeout=120)
        if run.returncode:
            raise ValueError(f"Clean latexmk failed: {run.stdout.decode(errors='replace')[-800:]}")
        recorded = (work / "main.fls").read_text(errors="replace").splitlines()
        paths = [(work / line.removeprefix("INPUT ")).resolve() for line in recorded if line.startswith("INPUT ")]
        if any(path.is_relative_to(project) for path in paths):
            raise ValueError("Clean build read from the original project through an absolute path")
        inputs = {path.name for path in paths}
        biblog = (work / "main.blg").read_text(errors="replace")
        if "icml2026.sty" not in inputs or "The style file: icml2026.bst" not in biblog:
            raise ValueError("The clean build did not use the ICML style and bibliography style")
        log = (work / "main.log").read_text(errors="replace")
        warnings = [line.strip() for line in log.splitlines()
                    if re.search(r"Overfull \\[hv]box|undefined (references|citations)|multiply defined", line, re.I)]
        if warnings:
            raise ValueError(f"Clean build has {len(warnings)} layout/reference warnings: " + " | ".join(warnings[:8]))
        # Image inspection in another check can populate a transformed
        # MuPDF image cache. Compare two pristine readers of the final PDFs.
        with fitz.open(stream=delivered_pdf, filetype="pdf") as doc, fitz.open(work / "main.pdf") as rebuilt:
            if len(rebuilt) != len(doc) or not all(a.get_pixmap().digest == b.get_pixmap().digest
                                                   for a, b in zip(doc, rebuilt)):
                raise ValueError("Clean build renders differently from delivered PDF")
            return {"identical_rendered_pages": len(doc)}


def scheduler(project, job_id, accounting):
    completed = [line.split("|")[:3] for line in accounting.read_text().splitlines()]
    if [job_id, "COMPLETED", "0:0"] not in [[s.strip() for s in row] for row in completed]:
        raise ValueError(f"No successful scheduler accounting record for job {job_id}")
    results = project / "results"
    logs = [p for base in (results, project / "logs", project / "auto_research/logs", project)
            for p in (base.glob(f"*{job_id}*") if base == project else base.rglob(f"*{job_id}*"))
            if p.is_file() and p.suffix in (".out", ".log")
            and re.search(rf"(?<!\d){re.escape(job_id)}(?!\d)", p.name)]
    runs = project / "auto_research/state/native_runs"
    for path in runs.glob("*/receipt.json"):
        row = json.loads(path.read_text())
        if (row.get("backend") == "slurm" and str(row.get("job_id")) == job_id
                and row.get("status") == "completed" and row.get("identity") == "i2p-" + path.parent.name
                and not row.get("input_sha256")):
            output = path.parent / "stdout"
            result = (project / row["artifact"]).resolve()
            if (output.is_file() and result.is_relative_to(results.resolve()) and result.is_file()
                    and sha256(result) == row.get("sha256")):
                logs.append(output)
    if not any(p.stat().st_size for p in logs):
        raise ValueError(f"No worker output file for scheduler job {job_id} in project")
    return {"job_id": job_id, "accounting_sha256": sha256(accounting),
            "worker_logs": [str(p.relative_to(project)) for p in logs]}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("case", choices=("digits", "friedman"))
    ap.add_argument("project", type=Path)
    ap.add_argument("--rows", required=True, type=Path, help="Experiment CSV or JSON, relative to project")
    ap.add_argument("--json-path", default="")
    ap.add_argument("--column", action="append", default=[], metavar="CANONICAL=ACTUAL")
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--sacct", required=True, type=Path)
    ap.add_argument("--replayed-plots", nargs=2, type=Path, metavar=("CLEAN_FIGURES", "PERTURBED_FIGURES"))
    ap.add_argument("--google-image-receipt", type=Path, action="append", default=[],
                    help="Authenticated compact image receipt with responseId, modelVersion and payloads[].sha256")
    ap.add_argument("--visual-review", type=Path, help="Independent diagram observations bound to the PDF and figure hashes")
    ap.add_argument("--google-response", required=True, type=Path, help="Saved authenticated Google interaction response")
    ap.add_argument("--output", required=True, type=Path)
    a = ap.parse_args()
    project = a.project.resolve()
    paper = project / "paper"
    measurements = (project / a.rows).resolve()
    if not measurements.is_relative_to(project):
        ap.error("--rows must point inside the project")
    columns = dict(v.split("=", 1) for v in a.column)
    checks = {}

    def check(name, fn, *args):
        try:
            detail = fn(*args)
            checks[name] = {"pass": True, "detail": detail}
            return detail
        except Exception as exc:
            checks[name] = {"pass": False, "detail": str(exc)}
            return None

    check("scheduler", scheduler, project, a.job_id, a.sacct)
    rows = check("experiment", lambda: replay(measured_rows(measurements, a.json_path), columns, reference_rows(a.case)))
    pdf = paper / "main.pdf"
    fingerprint = None
    try:
        delivered_pdf = pdf.read_bytes()
        fingerprint = hashlib.sha256(delivered_pdf).hexdigest()
        with fitz.open(stream=delivered_pdf, filetype="pdf") as doc:
            check("pagination", layout, doc)
            check("research_citations", check_sources, doc, paper, project / "auto_research/state", a.google_response)
            graphics = check("graphics", check_graphics, doc, paper, a.replayed_plots)
            check("method_diagram_science", check_diagram_review, paper, graphics, a.google_image_receipt)
            if a.visual_review:
                check("independent_visual_review", visual_review, a.visual_review, fingerprint, paper, graphics)
            check("clean_build", check_build, paper, delivered_pdf)
            check("rendered_measurements", displayed_metrics, doc, a.case, rows or {})
            if not rows:
                checks["rendered_measurements"] = {"pass": False, "detail": "No independently verified result rows."}
    except Exception as exc:
        checks["paper"] = {"pass": False, "detail": str(exc)}
    def delivery_contract():
        report = evaluate(project, 4)
        if report.violations:
            raise ValueError("; ".join(f"{v.check}: {v.detail}" for v in report.violations))
        return report.to_dict()

    check("delivery_contract", delivery_contract)
    passed = all(c["pass"] for c in checks.values())
    result = {"pass": passed, "case": a.case, "pdf_sha256": fingerprint, "checks": checks}
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(result, indent=2) + "\n")
    print("PASS" if passed else "FAIL", ", ".join(k for k, v in checks.items() if not v["pass"]))
    return int(not passed)


if __name__ == "__main__":
    sys.exit(main())
