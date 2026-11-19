"""Read-only, revision-bound evidence and verdicts for an independent reviewer.

Callers own the reviewer session, successful evidence-delivery receipts and
persisted records. Never accept a snapshot, receipt or prior record from the
researcher or from the reviewer model. Only the response is model-authored.
"""
from __future__ import annotations

import ast
import base64
import csv
from decimal import Decimal, InvalidOperation
import io
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import statistics
import sys
import textwrap
import unicodedata


DOMAINS = ("manuscript", "experiment", "citations", "visual")
STATE = "auto_research/state"
VIEW_SIZE = 8000
BLIND_SIZE = 12000
MAX_ARTIFACTS = 384
MAX_BYTES = 128 * 1024 * 1024
REVIEW_VERSION = 1


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _relative(value):
    if not isinstance(value, (str, Path)):
        raise ValueError("Expected a relative artifact path")
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or any(p.startswith(".") for p in path.parts):
        raise ValueError("Expected a project-relative public artifact path")
    return path


def _inside(root, relative):
    path = root / _relative(relative)
    if path.resolve() != path:
        raise ValueError(f"Linked review artifact: {relative}")
    return path


def _file_digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        st = os.fstat(stream.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            raise ValueError(f"Review artifact must be a regular file under {MAX_BYTES} bytes: {path.name}")
        while block := stream.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()


def _artifact_paths(root, paper):
    """Only include submission inputs, recorded measurements and public sources."""
    state = Path(STATE)
    for name, kind in {"idea.md": "brief", "experiment_plan.yaml": "protocol", "findings.yaml": "analysis",
                       "findings.json": "analysis", "project_context.md": "context", "deep_research.md": "context",
                       "literature.yaml": "context"}.items():
        if _inside(root, state / name).is_file():
            yield state / name, kind
    areas = (
        (paper, {".tex", ".bib", ".pdf", ".png", ".jpg", ".jpeg", ".svg", ".sty", ".cls", ".bst"}, "submission"),
        (Path("results"), {".json", ".jsonl", ".csv", ".tsv"}, "measurement"),
        (Path("sources"), {".txt", ".md", ".pdf"}, "primary_source"),
        (Path("experiments"), {".py", ".r", ".jl", ".sh"}, "method"),
        (Path("scripts"), {".py", ".r", ".jl", ".sh"}, "method"),
    )
    for folder, extensions, kind in areas:
        start = _inside(root, folder)
        if not start.is_dir():
            continue
        # Walk only scientific inputs. Ignore generated environments, build
        # trees and hidden directories even when the project is user supplied.
        for parent, directories, files in os.walk(start):
            directories[:] = sorted(d for d in directories if not d.startswith(".") and d not in
                                    ("__pycache__", "node_modules", "venv", "vendor")
                                    and not (Path(parent) / d).is_symlink())
            for name in sorted(files):
                path = Path(parent) / name
                ext = path.suffix.lower()
                if name.startswith(".") or ext not in extensions or re.search(
                    r"(?:^|[._-])(credentials?|secrets?|tokens?|environment_setup|config)(?:[._-]|$)", name, re.I
                ):
                    continue
                rel = path.relative_to(root)
                _inside(root, rel)
                if not path.is_file():
                    raise ValueError(f"Expected a regular artifact: {rel}")
                if kind == "submission" and path.parent == start and (
                    re.fullmatch(r"page_?\d+\.(png|jpg)", name, re.I)
                    or (ext == ".pdf" and rel != paper / "main.pdf")
                ):
                    continue
                yield rel, kind


def _figure_methods(root, paper, artifacts, registered):
    """Link included figures to their plotting source; also handle imported manuscripts."""
    from ark.citation import active_tex_sources

    included = set()
    for path, source in active_tex_sources(root / paper):
        if path.relative_to(root).as_posix() in artifacts:
            included.update(Path(name).name for name in re.findall(
                r"\\includegraphics\*?(?:\s*\[[^]]*\])?\s*\{([^{}]+)\}", source))
    if not included:
        return {}
    def included_name(filename):
        return filename in included or Path(filename).stem in included

    found = {name: method for name, method in registered.items()
             if included_name(name) and artifacts.get(method, {}).get("kind") == "method"}
    inferred = {}
    for name, meta in artifacts.items():
        path = root / name
        if meta["kind"] != "method" or path.suffix != ".py" or path.stat().st_size > 256_000:
            continue
        try:
            source = path.read_bytes()
            if b"savefig" not in source or _digest(source) != meta["sha256"]:
                continue
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            continue
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or getattr(call.func, "attr", "") != "savefig":
                continue
            arg = call.args[0] if call.args else next((kw.value for kw in call.keywords if kw.arg == "fname"), None)
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                filename = Path(arg.value).name
                if included_name(filename):
                    inferred.setdefault(filename, set()).add(name)
    # An old script may export the same figure; only a worker receipt can
    # disambiguate competing programs.
    for filename, names in inferred.items():
        if len(names) == 1:
            found.setdefault(filename, next(iter(names)))
    return found


def build_snapshot(project, *, paper_dir="paper", target_track="", required=("manuscript", "experiment"),
                   registered_plots=None, measurement_run=None, expected_measurement=None, measurement_comparison=None):
    """Fingerprint the submission and allowlisted evidence without exposing project secrets."""
    root = Path(project).resolve(strict=True)
    if not isinstance(required, (tuple, list)) or any(d not in DOMAINS for d in required):
        raise ValueError("Unknown required review domain")
    if not isinstance(target_track, str) or len(target_track) > 500:
        raise ValueError("Expected a short target track")
    paper = _relative(paper_dir)
    artifacts = {}
    for rel, kind in _artifact_paths(root, paper):
        if len(artifacts) >= MAX_ARTIFACTS:
            raise ValueError("Review evidence exceeds the supported artifact limit")
        path = _inside(root, rel)
        artifacts[rel.as_posix()] = {"kind": kind, "sha256": _file_digest(path)}
    manifest = {"version": REVIEW_VERSION, "artifacts": artifacts, "paper_dir": paper.as_posix(),
                "target_track": target_track, "required": sorted(set(required) | {"manuscript"})}
    run = measurement_run or {}
    if expected_measurement is not None:
        # A revised plan preserves earlier and ancillary datasets in results/.
        # Their contents can be compared, but only the current worker can attest to its output.
        bound = (run.get("status") == "completed" and run.get("artifact") == expected_measurement
                 and artifacts.get(expected_measurement) == {"kind": "measurement", "sha256": run.get("sha256")})
        manifest["worker_measurement"] = {"expected": expected_measurement, "verified": bound}
        for name, meta in artifacts.items():
            if meta["kind"] == "measurement" and not (bound and name == expected_measurement):
                meta["kind"] = "supporting_measurement"
        comparison = measurement_comparison or {}
        if (bound and comparison.get("unchanged_arrays") and comparison.get("current") == expected_measurement
                and comparison.get("current_sha256") == run["sha256"]
                and artifacts.get(comparison.get("previous")) == {
                    "kind": "supporting_measurement", "sha256": comparison.get("previous_sha256")}):
            manifest["measurement_comparison"] = comparison
    digest = run.get("code_sha256")
    if "experiment" in required and run.get("status") == "completed" and re.fullmatch(r"[0-9a-f]{64}", str(digest)):
        method = run.get("script")
        if not isinstance(method, str) or artifacts.get(method) != {"kind": "method", "sha256": digest}:
            method = None
            identity = run.get("identity") or ""
            if re.fullmatch(r"i2p-[a-f0-9]{24}", identity):
                sealed = f"{STATE}/native_runs/{identity[4:]}/input/main.py"
                path = _inside(root, sealed)
                if path.is_file() and _file_digest(path) == digest:
                    if len(artifacts) >= MAX_ARTIFACTS:
                        raise ValueError("Review evidence exceeds the supported artifact limit")
                    artifacts[sealed] = {"kind": "method", "sha256": digest}
                    method = sealed
        manifest["measurement_method"] = {"artifact": method, "sha256": digest}
        if method and method.endswith(".py") and run.get("backend") in ("local", "slurm"):
            manifest["method_control"] = {"version": 1, "backend": run["backend"]}
            if run["backend"] == "local":
                manifest["method_control"]["image"] = run.get("image")
        brief = f"{STATE}/idea.md"
        if brief in artifacts:
            manifest["measurement_method"]["request"] = brief
    if "method_control" in manifest and (comparison := manifest.get("measurement_comparison")):
        from ark.measurement_table import reproduction_row
        measured = comparison["current"]
        data = _artifact({**manifest, "project": str(root)}, measured)[0]
        row = reproduction_row(json.loads(data), comparison["unchanged_arrays"], comparison["current_sha256"])
        if row:
            manifest["measurement_reproduction"] = {"version": 2, "artifact": measured, "sha256": comparison["current_sha256"],
                "rows_pointer": row["rows_pointer"], "identifiers": row["identifiers"], "columns": list(row["outcomes"])}
    if "visual" in required and any(meta["kind"] == "measurement" for meta in artifacts.values()):
        methods = _figure_methods(root, paper, artifacts, registered_plots or {})
        if methods:
            manifest["figure_methods"] = methods
    pdf = (paper / "main.pdf").as_posix()
    if pdf in artifacts and any(meta["kind"] == "measurement" for meta in artifacts.values()):
        if conclusion := _rendered_conclusion({**manifest, "project": str(root)}, pdf):
            manifest["conclusion"] = conclusion
            worker = manifest.get("worker_measurement", {})
            if worker.get("verified") and expected_measurement.endswith(".json"):
                from ark.measurement_table import describe
                path = _inside(root, expected_measurement)
                if path.stat().st_size <= 4 * 1024 * 1024:
                    data, _ = _artifact({**manifest, "project": str(root)}, expected_measurement)
                    try:
                        tables = describe(json.loads(data))
                    except (ValueError, UnicodeDecodeError, RecursionError):
                        tables = []
                    if tables and len(_canonical(tables[:8])) <= VIEW_SIZE:
                        manifest["evidence_scope"] = {"version": 1, "tables": tables[:8]}
    return {**manifest, "project": str(root), "revision": _digest(_canonical(manifest))}


def _artifact(snapshot, artifact):
    _relative(artifact)
    meta = snapshot["artifacts"].get(artifact)
    if not meta:
        raise ValueError(f"Artifact outside the review snapshot: {artifact}")
    path = _inside(Path(snapshot["project"]), artifact)
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES or _digest(data) != meta["sha256"]:
        raise ValueError(f"Artifact changed since review started: {artifact}")
    return data, meta


def _rendered_conclusion(snapshot, pdf):
    """Locate a quotable opening paragraph under a unique rendered conclusion heading."""
    import fitz
    from ark.latex.utils import body_page_boundary

    data, _ = _artifact(snapshot, pdf)
    try:
        document = fitz.open(stream=data, filetype="pdf")
    except (RuntimeError, ValueError):
        return None
    heading = re.compile(r"(?m)^[ \t]*(?:[\d.]+\s+|[IVX]+[. ]\s*)?(?:conclusions?(?: and future work)?|concluding remarks)[ \t]*$", re.I)
    with document as doc:
        boundary = body_page_boundary(doc)
        end = min(len(doc), boundary[0] + int(any(boundary[1]))) if boundary else len(doc)
        headings = []
        for index in range(end):
            page = doc[index]
            blocks = [b[4] for b in page.get_text("blocks") if b[6] == 0]
            for n, block in enumerate(blocks):
                for match in heading.finditer(block):
                    paragraph = block[match.end():].strip() or (blocks[n + 1].strip() if n + 1 < len(blocks) else "")
                    # Use complete sentences up to the ordinary reference size limit.
                    stops = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", paragraph) if m.end() <= 1200]
                    quote = paragraph[:stops[-1]] if stops else ""
                    text = page.get_text()
                    title = text.find(match[0])
                    offset = text.find(quote, title + len(match[0])) if title >= 0 else -1
                    # Changing the required review evidence opens a new revision
                    # without replacing a previously delivered archived verdict.
                    headings.append({"version": 2, "artifact": pdf, "page": index + 1, "start": offset + 1,
                                     "quote": quote} if len(quote) >= 4 and offset >= 0 else None)
        return headings[0] if len(headings) == 1 else None


def _page_index(number, count):
    if type(number) is not int or not 1 <= number <= count:
        raise ValueError(f"Expected a page between 1 and {count}")
    return number - 1


def _measured_statistic(data, ext, statistic, column, rows_pointer):
    if statistic not in ("count", "min", "max", "sum", "mean", "median"):
        raise ValueError("Use count, min, max, sum, mean or median")
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("Use a tabular measurement summary under 4 MiB")
    if type(column) is not str or len(column) > 200 or (not column and statistic != "count"):
        raise ValueError("Select a measured column by exact name")
    if not isinstance(rows_pointer, str) or len(rows_pointer) > 400:
        raise ValueError("Invalid measurement rows pointer")
    if ext == ".json":
        from ark.findings_schema import _json_pointer
        rows = json.loads(data, parse_float=Decimal)
        if rows_pointer:
            if not rows_pointer.startswith("/"):
                raise ValueError("The measurement rows pointer must start with /")
            try:
                rows = _json_pointer(rows, rows_pointer)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ValueError("Measurement rows pointer was not found") from exc
    elif ext in (".csv", ".tsv") and not rows_pointer:
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")), delimiter="," if ext == ".csv" else "\t")
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("Measurement table must have unambiguous column names")
        rows = list(reader)
    else:
        raise ValueError("Statistics require a CSV/TSV table or JSON array of rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 50000 or not all(isinstance(r, dict) for r in rows):
        raise ValueError("Select a nonempty array of at most 50000 measurement rows")
    if column and any(column not in row for row in rows):
        raise ValueError("Requested column is missing in one or more measured rows")
    if statistic == "count":
        answer = len(rows)
    else:
        try:
            values = [Decimal(str(row[column])) for row in rows]
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("Every selected value must be numeric") from exc
        if any(not v.is_finite() or abs(v.adjusted()) > 300 for v in values):
            raise ValueError("Measurement contains a nonfinite or out-of-range value")
        operators = {"min": min, "max": max, "sum": sum, "median": statistics.median,
                     "mean": lambda xs: sum(xs) / len(xs)}
        answer = operators[statistic](values)
    return f"{statistic}({column or 'rows'}) = {answer} over {len(rows)} recorded rows"


def _text_window(text, start):
    if start > max(1, len(text)):
        raise ValueError("Evidence offset is past the end")
    end = min(start - 1 + VIEW_SIZE, len(text))
    return text[start - 1:end], end


def read_evidence(snapshot, artifact, *, page=None, start=1, visual=False, statistic=None, column="", rows_pointer=""):
    """Return a bounded text excerpt or an actual rendered PDF page with a receipt."""
    if type(start) is not int or start < 1 or type(visual) is not bool:
        raise ValueError("Invalid evidence offset or mode")
    data, meta = _artifact(snapshot, artifact)
    ext = Path(artifact).suffix.lower()
    receipt = {"revision": snapshot["revision"], "artifact": artifact, "sha256": meta["sha256"], "start": start}
    result = {"artifact": artifact, "sha256": meta["sha256"]}
    if statistic is not None:
        if meta["kind"] not in {"measurement", "supporting_measurement"} or visual or page is not None or start != 1:
            raise ValueError("Statistics operate on measured result artifacts only")
        value = _measured_statistic(data, ext, statistic, column, rows_pointer)
        receipt.update(statistic=statistic, column=column, rows_pointer=rows_pointer)
        return {**result, "text": value, "receipt": receipt}
    if column or rows_pointer:
        raise ValueError("Select a statistic to summarize measured rows")
    if ext == ".pdf":
        import fitz
        with fitz.open(stream=data, filetype="pdf") as doc:
            page = 1 if page is None else page
            pg = doc[_page_index(page, len(doc))]
            result.update(page=page, pages=len(doc))
            receipt["page"] = page
            if visual:
                if start != 1:
                    raise ValueError("Page images have no text offset")
                size = max(pg.rect.width, pg.rect.height)
                scale = min(1.75, 1440 / max(1.0, size))
                png = pg.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("png")
                result.update(mime_type="image/png", image_base64=base64.b64encode(png).decode("ascii"))
                receipt.update(visual=True, image_sha256=_digest(png))
                return {**result, "receipt": receipt}
            text = pg.get_text()
    else:
        if visual or page is not None or ext in {".png", ".svg", ".jpg", ".jpeg"}:
            raise ValueError("Inspect figures on their rendered manuscript page")
        text = data.decode("utf-8", errors="replace")
    chunk, receipt["end"] = _text_window(text, start)
    return {**result, "text": chunk, "next_start": start + len(chunk) if receipt["end"] < len(text) else None,
            "receipt": receipt}


def _submission(snapshot):
    return f"{snapshot['paper_dir']}/main.tex", f"{snapshot['paper_dir']}/main.pdf"


def _blind_text(snapshot):
    tex, _ = _submission(snapshot)
    if tex not in snapshot["artifacts"]:
        return ""
    return _artifact(snapshot, tex)[0].decode("utf-8", errors="replace")[:BLIND_SIZE]


# Match only explicit absolute changes with unitless endpoints. A fold change,
# relative percentage or a comparison of different estimands requires review.
_NUMBER = r"[+-]?(?:\d{1,12}(?:\.\d{1,12})?|\.\d{1,12})"
_SPREAD = rf"(?:\s*(?:\\pm|±)\s*{_NUMBER})?"
_ABSOLUTE_CHANGE = re.compile(
    rf"\bfrom\s+\$?(?P<before>{_NUMBER}){_SPREAD}\$?\s+to\s+"
    rf"\$?(?P<after>{_NUMBER}){_SPREAD}\$?\s*[,;(]\s*(?:an?\s+)?"
    rf"(?:(?:paired|absolute|mean)\s+)*(?P<direction>increase|decrease)\s+of\s+"
    rf"\$?(?P<effect>{_NUMBER}){_SPREAD}\$?(?=\s*[,;)\n]|\.(?:\s|$)|$)", re.I,
)


def reported_change_conflicts(source):
    """Explicit endpoint/difference triplets that disagree beyond the printed precision."""
    # Line comments often retain a superseded version of a scientific claim.
    visible = re.sub(r"(?<!\\)%[^\n]*", "%", source)
    conflicts = []
    for match in _ABSOLUTE_CHANGE.finditer(visible):
        first, last, effect = (Decimal(match[key]) for key in ("before", "after", "effect"))
        difference = last - first
        if match["direction"].lower() == "decrease" and effect >= 0:
            difference = -difference
        tolerance = sum(Decimal(1).scaleb(v.as_tuple().exponent) for v in (first, last, effect)) / 2
        wrong_direction = (difference < 0 if match["direction"].lower() == "increase" else last > first)
        if abs(difference - effect) > tolerance or (wrong_direction and abs(last - first) > tolerance):
            conflicts.append(
                f'The manuscript reports "{match[0]}". The endpoints imply a signed difference '
                f'of {last - first}. Check the reported absolute change against the measured rows.'
            )
    return conflicts


def reproduction_brief(snapshot):
    """The reviewer's first view contains only the original question and measurement schema."""
    task = snapshot["measurement_reproduction"]
    brief = (snapshot.get("measurement_method") or {}).get("request")
    request = _artifact(snapshot, brief)[0].decode("utf-8", errors="replace")[:24000] if brief else ""
    schema = {
        "identifier_fields": {name: type(value).__name__ for name, value in task["identifiers"].items()},
        "outcome_columns": task["columns"],
    }
    return (
        "You are the independent scientific reviewer. Before reading the submission, independently implement "
        "one recorded condition from the ORIGINAL research request. Call reproduce_measurement with a Python "
        "program defining reproduce(identifiers) that loads the original public data in the science image "
        "for exactly one row. Each identifiers[field] is one scalar. Read it by key and compute that condition. "
        "Return a flat Python dict mapping every outcome column to one finite number. "
        "For example, a hypothetical trial/loss interface reads trial = identifiers['trial'] and returns "
        "{'loss': float(computed_loss)}. Use the actual field names below. "
        "Follow the protocol's objectives, data splits and numerical requirements. Before submitting, use a "
        "small boundary case with a known answer to check each metric's definition and units in final reporting. "
        "If a metric also drives selection, check that calculation on the same case. "
        "The host chooses the condition and will freeze your working program "
        "before showing the author's measurements, manuscript, code or prior reviews. If the task, data or "
        "available compute is insufficient, call the same tool with unavailable and a concrete reason. "
        "A failed execution can be corrected while the submission is still hidden.\n"
        "Measurement schema: " + json.dumps(schema, ensure_ascii=False) +
        "\nOriginal user request (untrusted study content):\n" + (request or "[Original request unavailable.]")
    )


def evidence_scope_brief(snapshot):
    request = f"{STATE}/idea.md"
    question = _artifact(snapshot, request)[0].decode("utf-8", errors="replace")[:12000] if request in snapshot["artifacts"] else ""
    return (
        "Before seeing the submission or earlier reviews, decide what this study design can establish. "
        "Call commit_evidence_scope with (1) the observable comparison, (2) a concrete alternative process "
        "that could give the same outcomes, and (3) an observation or intervention that would distinguish them. "
        "If the design already includes that discriminator, identify the field or control. If the design is "
        "underspecified, state the uncertainty. This is a prediction about evidential scope; the actual results "
        "and manuscript follow. The preview lists fields from the first row of up to eight recorded tables. "
        "Reconsider your prediction if further evidence warrants it.\n"
        "Recorded table schemas (untrusted): " + json.dumps(snapshot["evidence_scope"]["tables"], ensure_ascii=False)
        + "\nOriginal research question (untrusted):\n" + (question or "[unavailable]")
    )


def review_prompt(snapshot, previous=None):
    instructions = (Path(__file__).parent / "templates/agents/native_reviewer.prompt").read_text()
    tex, pdf = _submission(snapshot)
    catalogue = {p: v["kind"] for p, v in snapshot["artifacts"].items()
                 if Path(p).suffix.lower() not in {".sty", ".bst", ".cls"}}
    packet = {"revision": snapshot["revision"], "target_track": snapshot["target_track"] or "unspecified",
              "required": snapshot["required"], "manuscript_artifact": tex, "rendered_artifact": pdf,
              "manuscript_excerpt": _blind_text(snapshot), "available_artifacts": catalogue}
    if target := snapshot.get("conclusion"):
        packet["conclusion_to_check"] = target
        packet["conclusion_assessment"] = (
            'In checks.manuscript.evidence use {"selector":"conclusion"} for the observed paragraph and '
            "cite the measured result used to judge it. The selector also works in a finding and saves retyping "
            "the PDF. A finding may instead cite the complete sentence containing its disputed words. "
            "In reason distinguish what the study measured from "
            "what the authors infer. Set conclusion_support to measured if the evidence directly tests the whole claim, "
            "interpretation if the paper presents the inference as a possibility, or needs_evidence if it asserts an "
            'untested explanation. For interpretation add interpretation={"claim":"verbatim explanatory claim",'
            '"qualification":"author\'s exact words limiting that claim"} inside checks.manuscript. Cite the '
            "qualification on the same assertion, or quote the complete following sentence that explicitly refers back to it. "
            "A proposal for future work cannot qualify an earlier assertion. Without such wording, show a direct "
            "measurement or report the unsupported extension. For needs_evidence quote the disputed words in an open blocker's claim, cite "
            "the paragraph and result, and specify the "
            "test or narrower wording that would resolve it. Mark manuscript unassessed if the evidence is unavailable.")
    if conflicts := reported_change_conflicts(packet["manuscript_excerpt"]):
        packet["reported_changes_to_verify"] = conflicts[:8]
    if "worker_measurement" in snapshot:
        packet["worker_measurement"] = snapshot["worker_measurement"]
        packet["measurement_guidance"] = ("Cite the current worker output for the experiment and its plotted values. "
                                          "Other result files are available for explicit comparisons.")
    if "measurement_comparison" in snapshot:
        packet["measured_revision_comparison"] = snapshot["measurement_comparison"]
    if snapshot.get("measurement_method"):
        packet["measurement_method"] = snapshot["measurement_method"]
        packet["experiment_evidence_required"] = (
            "Compare the requested measurement with the implementation that produced the rows. "
            "Check the split, objective, units, numerical transformations and aggregation. "
            "Follow local helper calls to the operations that compute the quantity. Compare their behavior "
            "on an edge case with the requested definition. In checks.experiment.evidence cite the defining "
            "calculation, a recorded row and the applicable "
            "passage of the request when present. A reproducible computation can still measure a different quantity. "
            "Mark experiment unassessed if the executed source is unavailable.")
    if snapshot.get("method_control") and not snapshot.get("measurement_reproduction"):
        packet["independent_method_control"] = (
            "Use run_method_control before assessing the experiment. Derive the prediction from the brief, "
            "before seeing output. Exercise the central disputed calculation (or model selection that depends "
            "on it). Choose an input on which a plausible alternative implementation would give a different "
            "answer, including meaningful boundary values, regimes or opposing observations. Normal examples "
            "on which both methods agree cannot distinguish them. Supply the independent derivation and the "
            "competing prediction in rationale. A mismatch requires a located experiment blocker or a specific "
            "unassessed explanation if the interpretation is uncertain. If the method cannot be exercised, "
            "mark experiment unassessed. The host retains observed controls in the verdict.")
    if task := snapshot.get("measurement_reproduction"):
        packet["independent_reproduction"] = {"artifact": task["artifact"], "rows_pointer": task["rows_pointer"],
                                               "identifier_fields": list(task["identifiers"]), "columns": task["columns"]}
        packet["reproduction_instructions"] = (
            "The comparison labels each value by its source. Agreement uses the host's numerical tolerance. "
            "Both implementations can share an error. Cite the worker's recorded value and your "
            "independently recomputed value by name. Investigate any disagreement against the original "
            "metric definition and the executed source. Your own program can also be wrong. Report a blocker "
            "when the evidence locates a defect in the submission. Attach the relevant original definition, "
            "executed operation and measured row to that finding so the author can inspect the diagnosis. "
            "If the cause is unresolved, your program "
            "is unsound, or independent reproduction was unavailable, mark the experiment unassessed and explain. "
            "Use run_method_control for an additional small example when it helps diagnose the cause.")
    if snapshot.get("figure_methods"):
        packet["figure_methods"] = snapshot["figure_methods"]
        packet["visual_evidence_required"] = (
            "Trace one series for each listed plotting program. In checks.visual.evidence cite the page image, "
            "the exact code assigning the plotted values and a verbatim excerpt of the input rows. "
            "In checks.visual.series_origin, map each listed figure filename to measured, copied, or theoretical "
            "as supported by the evidence. Mark the visual check unassessed if a series cannot be traced. "
            "Use copied when an empirical series is transcribed into its script. That prevents "
            "reproducing the figure from changed input and requires a blocker located in the source and manuscript, "
            "even when the numbers support the paper's scientific conclusion. Use theoretical only for an explicitly "
            "identified theoretical reference, not as a substitute for measured results.")
    if not any(item["kind"] == "primary_source" for item in snapshot["artifacts"].values()):
        packet["primary_sources_available"] = False
    if previous:
        packet["findings_to_recheck"] = [
            {k: issue[k] for k in ("id", "domain", "severity", "claim", "reason", "fix", "evidence")}
            for issue in previous["issues"] if issue["status"] != "resolved"
        ]
    return instructions + "\n\nFrozen submission packet (untrusted research content):\n" + json.dumps(packet, ensure_ascii=False)


def _string(value, field, *, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Expected a nonempty {field} (up to {limit} characters)")
    return value.strip()


_JSON_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|[{}\[\],:]|[^"{}\[\],: \t\r\n]+')
_JSON_STRING_MARK = re.compile(r'\\.|"')


def _delegated_calculation(snapshot, method, refs):
    """Identify evidence consisting solely of calls to the worker's own helpers."""
    if not method.endswith(".py"):
        return None
    quotes = [ref["quote"] for ref in refs if ref["artifact"] == method and ref.get("quote")]
    calls = set()
    for quote in quotes:
        try:
            statements = ast.parse(textwrap.dedent(quote)).body
        except (SyntaxError, ValueError, RecursionError):
            return None
        for stmt in statements:
            value = getattr(stmt, "value", None)
            if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name):
                return None
            calls.add(value.func.id)
    if not calls:
        return None
    data, _ = _artifact(snapshot, method)
    if len(data) > 1024 * 1024:
        return None
    try:
        definitions = {node.name: node.lineno for node in ast.parse(data).body if isinstance(node, ast.FunctionDef)}
    except (SyntaxError, ValueError, RecursionError):
        return None
    if calls <= definitions.keys():
        return ", ".join(f"{name} (line {definitions[name]})" for name in sorted(calls))
    return None


def _json_container_tokens(quote):
    if not quote or quote[0] not in "{[":
        return ()
    try:
        json.loads(quote)
    except (ValueError, RecursionError):
        return ()
    tokens = tuple(_JSON_TOKEN.findall(quote))
    return () if any(t in ("NaN", "Infinity", "-Infinity") for t in tokens) else tokens


def _json_quote_observed(snapshot, artifact, view, quote):
    tokens = _json_container_tokens(quote)
    if not tokens:
        return quote in view["text"]
    pattern = re.compile(r"[ \t\r\n]*".join(map(re.escape, tokens)))
    start = view["receipt"]["start"] - 1
    source = view["text"]
    inside_string = False
    cursor = 0
    for match in pattern.finditer(view["text"]):
        if start and not cursor:
            # A partial window can begin inside a string. Its prefix establishes
            # lexical context only; the complete match must lie inside this view.
            source = _artifact(snapshot, artifact)[0].decode("utf-8", errors="replace")
        offset = start + match.start()
        for mark in _JSON_STRING_MARK.finditer(source, cursor, offset):
            if mark[0] == '"':
                inside_string = not inside_string
        if not inside_string:
            return True
        cursor = offset
    return False


def _observed_pdf_windows(snapshot, artifact, observed):
    """Replay delivered text windows, opening the frozen PDF once across pages."""
    identity = {"revision": snapshot["revision"], "artifact": artifact,
                "sha256": snapshot["artifacts"][artifact]["sha256"]}
    receipt_keys = identity.keys() | {"page", "start", "end"}
    by_page = {}
    for receipt in observed:
        if not isinstance(receipt, dict) or receipt.keys() != receipt_keys:
            continue
        page, start = receipt["page"], receipt["start"]
        if (all(receipt[k] == v for k, v in identity.items()) and type(page) is int and page > 0
                and type(start) is int and start > 0 and type(receipt["end"]) is int):
            by_page.setdefault(page, set()).add((start, receipt["end"]))
    if not by_page:
        return
    import fitz
    with fitz.open(stream=_artifact(snapshot, artifact)[0], filetype="pdf") as doc:
        for page, ranges in by_page.items():
            if page > len(doc):
                continue
            text = doc[page - 1].get_text()
            for start, end in ranges:
                if start > max(1, len(text)):
                    continue
                window, expected_end = _text_window(text, start)
                if expected_end == end:
                    yield page, start, window


def _observed_conclusion(snapshot, observed):
    target = snapshot.get("conclusion")
    if not target:
        raise ValueError("This snapshot has no conclusion selector.")
    artifact, quote = target["artifact"], target["quote"]
    for page, start, window in _observed_pdf_windows(snapshot, artifact, observed):
        offset = target["start"] - start
        if page == target["page"] and offset >= 0 and window[offset:offset + len(quote)] == quote:
            return {"artifact": artifact, "page": page, "quote": quote,
                    "sha256": snapshot["artifacts"][artifact]["sha256"]}
    raise ValueError(f'The conclusion selector needs an observed text window from this revision. '
                     f'Read {artifact}, page {target["page"]}, start {target["start"]}, visual=false.')


def _references(snapshot, refs, observed, blind, *, required, pdf_windows=None):
    if not isinstance(refs, list) or len(refs) > 24 or (required and not refs):
        raise ValueError("Provide specific evidence for this judgment")
    tex, _ = _submission(snapshot)
    verified = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise ValueError("Expected an evidence reference")
        if "selector" in ref:
            if ref["selector"] != "conclusion" or ref.keys() - {"selector", "artifact", "page"}:
                raise ValueError('Use {"selector":"conclusion"} with only its optional artifact and page.')
            located = _observed_conclusion(snapshot, observed)
            if ("artifact" in ref and ref["artifact"] != located["artifact"]) or (
                    "page" in ref and (type(ref["page"]) is not int or ref["page"] != located["page"])):
                raise ValueError("The supplied conclusion locator conflicts with the observed PDF artifact or page.")
            verified.append(located)
            continue
        artifact = _string(ref.get("artifact"), "artifact path")
        info = snapshot["artifacts"].get(artifact)
        if not info:
            raise ValueError(f"Unknown evidence artifact: {artifact}")
        page = ref.get("page")
        visual = ref.get("visual", False)
        if type(visual) is not bool or (page is not None and (type(page) is not int or page < 1)):
            raise ValueError("Invalid evidence page or visual mode")
        if (Path(artifact).suffix.lower() == ".pdf") != (page is not None):
            raise ValueError("PDF references require a one-based page; text references use a quote")
        page_receipts = [r for r in observed if isinstance(r, dict) and not r.get("control") and
                         (r.get("revision"), r.get("artifact"), r.get("sha256"), r.get("page")) ==
                         (snapshot["revision"], artifact, info["sha256"], page)]
        receipt_matches = [r for r in page_receipts if r.get("visual", False) == visual]
        if visual:
            if not receipt_matches or not any(r.get("image_sha256") for r in receipt_matches):
                raise ValueError("The reviewer did not observe pixels of the cited page")
            verified.append({"artifact": artifact, "page": page, "visual": True, "sha256": info["sha256"],
                             "image_sha256": receipt_matches[0]["image_sha256"]})
            continue
        quote = _string(ref.get("quote"), "verbatim evidence quote", limit=1200)
        if len(quote) < 4:
            raise ValueError("Evidence quote is too short to locate")
        located = artifact == tex and _contains_observed_quote(blind, quote)
        calculation = {}
        for r in receipt_matches:
            if located:
                break
            options = {k: r[k] for k in ("statistic", "column", "rows_pointer") if k in r}
            view = read_evidence(snapshot, artifact, page=page, start=r.get("start", 1), **options)
            if view["receipt"] != r:
                continue
            if Path(artifact).suffix.lower() == ".json" and not options:
                located = _json_quote_observed(snapshot, artifact, view, quote)
            else:
                located = _contains_observed_quote(view["text"], quote)
            if located:
                calculation = options
        if not located:
            if page is not None and pdf_windows is not None:
                if artifact not in pdf_windows:
                    pdf_windows[artifact] = list(_observed_pdf_windows(snapshot, artifact, observed))
                matches = {p for p, _, text in pdf_windows[artifact] if p != page and quote in text}
                if len(matches) == 1:
                    raise ValueError(f"Retry the quote with observed page {matches.pop()} "
                                     f"(cited page {page}) in {artifact}: {quote[:100]}")
                if any(p == page for p, _, _ in pdf_windows[artifact]):
                    target = snapshot.get("conclusion") or {}
                    hint = (' For the observed conclusion paragraph use {"selector":"conclusion"}.'
                            if (artifact, page) == (target.get("artifact"), target.get("page")) else "")
                    raise ValueError(f"Quote the delivered text of page {page} verbatim, or read another text window "
                                     f"from {artifact}. Unlocated quote: {quote[:100]}{hint}")
                if any(r.get("visual") is True and r.get("image_sha256") for r in page_receipts):
                    raise ValueError(f"Only pixels were observed on page {page}. Call "
                                     f"read_evidence(artifact={json.dumps(artifact)}, page={page}, visual=false) "
                                     "and quote the returned text.")
            raise ValueError(f"Unobserved or unlocatable quote in {artifact}: {quote[:100]}")
        verified.append({"artifact": artifact, **({"page": page} if page else {}),
                         "quote": quote, "sha256": info["sha256"], **calculation})
    return verified


def draft_evidence_feedback(snapshot, proposal, observed, first_error):
    """Report independent evidence repairs together, without altering any proposed finding."""
    if not isinstance(proposal, dict):
        return first_error
    checks, issues = proposal.get("checks"), proposal.get("issues")
    if not isinstance(checks, dict) or not isinstance(issues, list):
        return first_error
    blind = _blind_text(snapshot)
    repairs = [first_error]
    manuscript = checks.get("manuscript")
    if (snapshot.get("conclusion") and isinstance(manuscript, dict) and manuscript.get("status") == "assessed"
            and manuscript.get("conclusion_support") not in ("measured", "interpretation", "needs_evidence")
            and "conclusion_support" not in first_error):
        repairs.append("checks.manuscript.conclusion_support is missing or invalid. Assess the whole conclusion "
                       "as measured, interpretation or needs_evidence, including any inference beyond the outcome.")
    pdf_windows = {}
    groups = [(f"checks.{name}", checks.get(name)) for name in ("manuscript", "experiment", "citations", "visual")]
    groups.extend((f"issues[{i}]", item) for i, item in enumerate(issues[:8]))
    for field, group in groups:
        if len(repairs) >= 6:
            break
        if not isinstance(group, dict) or not isinstance(group.get("evidence"), list):
            continue
        refs = group["evidence"]
        try:
            verified = _references(snapshot, refs, observed, blind, required=False, pdf_windows=pdf_windows)
        except ValueError as exc:
            message = str(exc)
        else:
            if not (field.startswith("issues[") and verified and not _covers(snapshot, "manuscript", verified)):
                continue
            message = "Locate the disputed passage in the manuscript or cite its observed PDF page."
        if message != first_error:
            repairs.append(f"{field}: {message[:240]}")
    return "\n".join(repairs)


def _covers(snapshot, domain, refs):
    tex, pdf = _submission(snapshot)
    if domain == "manuscript":
        return any(r["artifact"] in (tex, pdf) for r in refs)
    if domain == "visual":
        return any(r["artifact"] == pdf and r.get("visual") for r in refs)
    kind = "measurement" if domain == "experiment" else "primary_source"
    return any(snapshot["artifacts"][r["artifact"]]["kind"] == kind for r in refs)


def _copied_series_page(snapshot, refs, check):
    """Reuse a reviewer's page citation only when the disputed plot is painted on that page."""
    import fitz

    tex, pdf = _submission(snapshot)
    citations = check["evidence"]
    methods = {r["artifact"] for r in refs if r in citations}
    copied = {name for name, kind in check.get("series_origin", {}).items()
              if kind == "copied" and snapshot["figure_methods"][name] in methods}
    if not copied:
        return None
    linked = set()
    for r in refs:
        if r["artifact"] != tex:
            continue
        for literal in re.findall(r"\\includegraphics\*?(?:\s*\[[^]]*\])?\s*\{([^{}]+)\}", r.get("quote", "")):
            key = snapshot["paper_dir"] + "/" + literal
            if key in snapshot["artifacts"] and Path(key).name in copied:
                linked.add(key)
    if len(linked) != 1:
        return None
    key = linked.pop()
    if Path(key).suffix.lower() not in (".png", ".jpg", ".jpeg"):
        return None  # A vector figure still needs an explicit rendered page reference.
    try:
        image = fitz.Pixmap(_artifact(snapshot, key)[0])
        if image.alpha:
            image = fitz.Pixmap(image, 0)
        with fitz.open(stream=_artifact(snapshot, pdf)[0], filetype="pdf") as doc:
            pages = [r for r in citations if r["artifact"] == pdf and r.get("visual") and any(
                (entry["width"], entry["height"], entry["digest"]) == (image.width, image.height, image.digest)
                for entry in doc[r["page"] - 1].get_image_info(hashes=True))]
    except (RuntimeError, ValueError):
        return None
    return pages[0] if len(pages) == 1 else None


def _normalized_quote(value):
    # PDF extraction can change a printed ligature or wrap a line. Keep numbers,
    # units, words and mathematical relations intact when locating the passage.
    return " ".join(unicodedata.normalize("NFKC", value).replace("\u00ad", "").split())


def _contains_observed_quote(text, quote):
    return _normalized_quote(quote) in _normalized_quote(text)


def _quotes_conclusion(refs, target, *, disputed=None):
    paragraph = _normalized_quote(target["quote"])
    for ref in refs:
        if ref["artifact"] != target["artifact"] or ref.get("page") != target["page"]:
            continue
        quote = _normalized_quote(ref.get("quote", ""))
        if paragraph in quote:
            return disputed is None or _normalized_quote(disputed) in paragraph
        if (disputed is not None and _normalized_quote(disputed) in quote and quote.endswith((".", "!", "?"))
                and re.search(r"(?:^|(?<=[.!?]) )" + re.escape(quote) + r"(?= |$)", paragraph)):
            return True
    return False


def _interpretation_wording(target, evidence):
    """Bind the reviewer's reading to the author's words without classifying scientific prose."""
    if not isinstance(evidence, dict) or evidence.keys() != {"claim", "qualification"}:
        raise ValueError("For interpretation, give checks.manuscript.interpretation with verbatim claim and "
                         "qualification. Without an author-qualified inference, substantiate it or report a blocker.")
    claim = _string(evidence["claim"], "interpretation claim", limit=1200)
    qualification = _string(evidence["qualification"], "author's qualification", limit=1200)
    phrase, wording = _normalized_quote(claim), _normalized_quote(qualification)
    sentences = re.split(r'(?<=[.!?]) +(?=[A-Z“"‘])', _normalized_quote(target["quote"]))
    def located(excerpt, text):
        return re.search(r"(?<!\w)" + re.escape(excerpt) + r"(?!\w)", text) is not None

    if len(phrase) < 4 or not re.search(r"\w", wording):
        raise ValueError("Quote the explanatory claim and the words that limit it.")
    for i, sentence in enumerate(sentences):
        if not located(phrase, sentence):
            continue
        clauses = re.split(r";\s*|,\s+(?=(?:and|but|whereas|while|yet)\b)", sentence, flags=re.I)
        if phrase != wording and any(located(phrase, clause) and located(wording, clause) for clause in clauses):
            return {"claim": claim, "qualification": qualification}
        # A separate qualifying sentence can repeat the referent or explicitly
        # point back to it. An unrelated later use of the same term cannot qualify
        # its first assertion.
        following = sentences[i + 1] if i + 1 < len(sentences) else ""
        refers_back = re.match(r"^(?:it\b|(?:this|that|these|those) "
                               r"(?:explanations?|interpretations?|hypothes[ei]s|claims?|accounts?)\b)", wording, re.I)
        if following == wording and (wording.casefold().startswith(phrase.casefold()) or refers_back):
            return {"claim": claim, "qualification": qualification}
        break
    raise ValueError("Quote the author's qualifying words at the assertion they actually limit. "
                     "An unrelated sentence or future proposal cannot qualify the conclusion claim.")


def _passage_survives(snapshot, ref):
    """An unrelated edit or line reflow does not repair a disputed passage."""
    path = ref["artifact"]
    meta = snapshot["artifacts"].get(path)
    if not meta:
        return False
    if meta["sha256"] == ref["sha256"]:
        return True
    if ref.get("visual"):
        try:
            page = read_evidence(snapshot, path, page=ref["page"], visual=True)
        except ValueError:
            return False
        return page["receipt"]["image_sha256"] == ref["image_sha256"]
    data = _artifact(snapshot, path)[0]
    if path.lower().endswith(".pdf"):
        import fitz
        with fitz.open(stream=data, filetype="pdf") as doc:
            text = " ".join(page.get_text() for page in doc)
    else:
        text = data.decode("utf-8", "replace")
    return " ".join(ref["quote"].split()) in " ".join(text.split())


def _resolution_changed(snapshot, previous, old, refs):
    if previous["revision"] == snapshot["revision"]:
        return False
    submission = _submission(snapshot)
    if any(not _passage_survives(snapshot, r) for r in old["evidence"] if r["artifact"] in submission):
        return True
    # A previously missing control or source can support an unchanged claim.
    # Require a changed *evidence artifact* and a newly located observation;
    # changing an unrelated sentence in the TeX file cannot clear a blocker.
    def observation(r):
        quote = r.get("quote")
        if r["artifact"].lower().endswith(".json"):
            quote = _json_container_tokens(quote) or quote
        return r["artifact"], r.get("page"), quote

    prior_observations = {observation(r) for r in old["evidence"]}
    return any(r["artifact"] not in submission and
               r["sha256"] != previous["artifact_hashes"].get(r["artifact"]) and
               observation(r) not in prior_observations for r in refs)


def _score(value):
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10:
        raise ValueError("Review score must be a finite number between 0 and 10")
    return value


def validate_review(snapshot, response, previous=None, *, observed=()):
    """Check references, lifecycle and delivery eligibility; never infer scientific truth from a score."""
    if not isinstance(response, dict) or response.get("revision") != snapshot["revision"]:
        raise ValueError("The review does not identify the current revision")
    blind = _blind_text(snapshot)
    score = _score(response.get("score"))
    summary = _string(response.get("summary"), "central contribution")
    alternative = _string(response.get("alternative"), "competing explanation or limitation")
    strengths = response.get("strengths")
    if not isinstance(strengths, list) or not 1 <= len(strengths) <= 8:
        raise ValueError("Describe at least one evidenced strength or the strongest available observation")
    strengths = [_string(v, "strength") for v in strengths]
    raw_checks = response.get("checks")
    if not isinstance(raw_checks, dict) or set(raw_checks) != set(DOMAINS):
        raise ValueError("Return one check for each of manuscript, experiment, citations and visual")
    checks = {}
    controls = [r for r in observed if r.get("control") == 1 and r.get("revision") == snapshot["revision"]
                and r.get("sha256") == (snapshot.get("measurement_method") or {}).get("sha256")
                and r.get("artifact") == (snapshot.get("measurement_method") or {}).get("artifact")]
    task = snapshot.get("measurement_reproduction")
    reproductions = [r for r in observed if task and r.get("control") == 2 and r.get("revision") == snapshot["revision"]
                     and (r.get("artifact"), r.get("sha256")) == (task["artifact"], task["sha256"])]
    for domain in DOMAINS:
        check = raw_checks[domain]
        if not isinstance(check, dict) or check.get("status") not in ("assessed", "unassessed"):
            raise ValueError(f"Invalid {domain} assessment")
        assessed = check["status"] == "assessed"
        status = check["status"]
        reason = _string(check.get("reason"), "check reason")
        if (domain == "citations" and domain not in snapshot["required"] and assessed
                and check.get("evidence", []) == [] and not any(
                    m["kind"] == "primary_source" for m in snapshot["artifacts"].values())):
            # A source-free paper is commonly returned as "assessed, no
            # citations". With no original in scope the reproduction check
            # has no observed result, regardless of the model's wording.
            assessed, status = False, "unassessed"
            reason = "No primary-source passage is present in the snapshot. " + reason
        refs = _references(snapshot, check.get("evidence", []), observed, blind, required=assessed)
        origins = None
        if assessed and domain == "experiment" and (worker := snapshot.get("worker_measurement")):
            if not worker["verified"]:
                raise ValueError("The current plan has no verified worker output. Mark experiment unassessed.")
            if not any(r["artifact"] == worker["expected"] for r in refs):
                raise ValueError(f"Cite measured rows from the current worker output {worker['expected']}.")
        if assessed and not _covers(snapshot, domain, refs):
            raise ValueError(f"No observed {domain} evidence supports this assessment")
        if assessed and domain == "experiment" and (method := snapshot.get("measurement_method")):
            paths = {r["artifact"] for r in refs}
            if not method["artifact"]:
                raise ValueError("The worker's executed source is unavailable. Mark experiment unassessed.")
            if method["artifact"] not in paths or (method.get("request") and method["request"] not in paths):
                raise ValueError("Trace the measured quantity to its requested definition, executed calculation "
                                 "and recorded rows in checks.experiment.evidence.")
            if delegated := _delegated_calculation(snapshot, method["artifact"], refs):
                raise ValueError(f"The quoted code delegates the calculation to {delegated}. Inspect that "
                                 "definition and quote the operation that produces the measured quantity.")
        if domain == "experiment" and assessed and not task and snapshot.get("method_control") and not controls:
            raise ValueError("Run an independently predicted case with run_method_control on the frozen "
                             "calculation, or mark experiment unassessed.")
        if domain == "experiment" and assessed and task and not reproductions:
            raise ValueError("The revised worker returned unchanged measurements. Independently reproduce the "
                             "designated real row with reproduce_measurement, or mark experiment unassessed.")
        if assessed and domain == "visual":
            if methods := snapshot.get("figure_methods"):
                cited = {ref["artifact"] for ref in refs}
                missing = sorted(set(methods.values()) - cited)
                if missing or not any(snapshot["artifacts"][item]["kind"] == "measurement" for item in cited):
                    raise ValueError("Trace the plotted values in checks.visual.evidence: cite the rendered page, "
                                     "a verbatim measurement excerpt and the source assigning each plotted series in "
                                     + ", ".join(missing or sorted(set(methods.values()))))
                origins = check.get("series_origin")
                if not isinstance(origins, dict) or set(origins) != set(methods) or any(
                    value not in ("measured", "copied", "theoretical") for value in origins.values()
                ):
                    raise ValueError("For each figure in figure_methods, report measured, copied, or theoretical "
                                     "in checks.visual.series_origin. Mark the visual check unassessed when its origin is unknown.")
            _, pdf = _submission(snapshot)
            import fitz
            with fitz.open(stream=_artifact(snapshot, pdf)[0], filetype="pdf") as doc:
                seen = {r.get("page") for r in observed if r.get("revision") == snapshot["revision"]
                        and r.get("artifact") == pdf and r.get("sha256") == snapshot["artifacts"][pdf]["sha256"]
                        and r.get("visual") is True and r.get("image_sha256")}
                if not set(range(1, len(doc) + 1)).issubset(seen):
                    raise ValueError("Rendered layout was not observed on every manuscript page")
        checks[domain] = {"status": status, "reason": reason, "evidence": refs}
        if domain == "manuscript" and assessed and (target := snapshot.get("conclusion")):
            support = check.get("conclusion_support")
            if (not _quotes_conclusion(refs, target, disputed="" if support == "needs_evidence" else None)
                    or not _covers(snapshot, "experiment", refs)):
                raise ValueError(f"In checks.manuscript.evidence cite the supplied conclusion paragraph on PDF page "
                                 f'{target["page"]} with {{"selector":"conclusion"}} and cite an observed result.')
            if support == "needs_evidence" and not _quotes_conclusion(refs, target):
                _observed_conclusion(snapshot, observed)  # Check the context of a shorter disputed sentence.
            if support not in ("measured", "interpretation", "needs_evidence"):
                raise ValueError("Set checks.manuscript.conclusion_support to measured, interpretation, or "
                                 "needs_evidence, and explain the relationship to the measured result in reason.")
            checks[domain]["conclusion_support"] = support
            if support == "interpretation":
                checks[domain]["interpretation"] = _interpretation_wording(target, check.get("interpretation"))
        if domain == "experiment" and (controls or reproductions):
            checks[domain]["controls"] = controls + reproductions
        if origins:
            checks[domain]["series_origin"] = origins

    prior = {i["id"]: i for i in previous["issues"]} if previous else {}
    prior_open = {key for key, issue in prior.items() if issue["status"] != "resolved"}
    closed_ids = set(previous.get("closed_ids", [])) if previous else set()
    raw_issues = response.get("issues")
    if not isinstance(raw_issues, list) or len(raw_issues) > 32:
        raise ValueError("Expected an array of at most 32 review findings")
    issues = []
    missing_observations = False
    used = set()
    for issue in raw_issues:
        if not isinstance(issue, dict):
            raise ValueError("Expected a review finding")
        ident = issue.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"R[1-9][0-9]{0,3}", ident) or ident in used:
            raise ValueError("Finding IDs must be unique and use R1, R2, ...")
        used.add(ident)
        status, severity, domain = (issue.get(k) for k in ("status", "severity", "domain"))
        if status not in ("new", "still_present", "resolved", "unassessed") or severity not in (
                "blocker", "question", "minor") or domain not in DOMAINS:
            raise ValueError(f"Invalid status, severity or domain for {ident}")
        old = prior.get(ident)
        if ident in closed_ids or (not old and status not in ("new", "unassessed")) or (old and status == "new"):
            raise ValueError(f"{ident} has invalid revision history")
        if old and (old["domain"] != domain or old["severity"] != severity):
            raise ValueError(f"Cannot silently change the domain or severity of {ident}")
        supplied = issue.get("evidence", [])
        # A reviewer can correctly identify a missing control yet call its
        # absence a new scientific defect. Keep it delivery-blocking, distinguish
        # missing evidence from observed contradiction, and locate the disputed
        # quotation only when it actually occurs in the frozen manuscript.
        if severity == "blocker" and status in ("new", "still_present", "unassessed") and checks[domain]["status"] == "unassessed":
            if not supplied:
                claim = _string(issue.get("claim"), "disputed passage", limit=1200)
                if claim in blind:
                    supplied = [{"artifact": _submission(snapshot)[0], "quote": claim}]
            status = "unassessed"
            missing_observations = True
        refs = _references(snapshot, supplied, observed, blind, required=True)
        if not _covers(snapshot, "manuscript", refs):
            raise ValueError(f"{ident} must locate the disputed manuscript passage or page")
        if domain == "visual" and severity == "blocker" and status in ("new", "still_present") and not _covers(snapshot, domain, refs):
            if page := _copied_series_page(snapshot, refs, checks[domain]):
                refs.append(page)
        if status != "unassessed" and severity == "blocker" and (
                checks[domain]["status"] != "assessed" or not _covers(snapshot, domain, refs)):
            raise ValueError(f"Missing {domain} observation for blocking finding {ident}")
        if status == "resolved":
            if checks[domain]["status"] != "assessed" or not _covers(snapshot, domain, refs):
                raise ValueError(f"Resolving {ident} requires new verified evidence")
            if not _resolution_changed(snapshot, previous, old, refs):
                raise ValueError(f"No changed observation supports resolving {ident}")
        issues.append({"id": ident, "status": status, "severity": severity, "domain": domain,
                       **{k: _string(issue.get(k), k) for k in ("claim", "reason", "fix")}, "evidence": refs})
    if not prior_open.issubset(used):
        raise ValueError("Recheck every open finding from the preceding revision")
    if checks["manuscript"].get("conclusion_support") == "needs_evidence" and not any(
        issue["severity"] == "blocker" and issue["status"] in ("new", "still_present")
        and _quotes_conclusion(issue["evidence"], snapshot["conclusion"], disputed=issue["claim"])
        and _quotes_conclusion(checks["manuscript"]["evidence"], snapshot["conclusion"], disputed=issue["claim"])
        and _covers(snapshot, "experiment", issue["evidence"])
        for issue in issues
    ):
        raise ValueError("Report the unsupported conclusion as an open blocker. Put the disputed words in "
                         "claim verbatim, cite the same paragraph and its measured evidence, and specify "
                         "the missing test or wording change.")
    for name, origin in checks["visual"].get("series_origin", {}).items():
        if origin == "copied" and not any(
            issue["severity"] == "blocker" and issue["status"] in ("new", "still_present")
            and snapshot["figure_methods"][name] in {r["artifact"] for r in issue["evidence"]}
            for issue in issues
        ):
            raise ValueError(f"Report the copied empirical series in {name} as a reproducibility blocker. "
                             "Cite the plotting source and the figure's manuscript passage or rendered page.")
    if any(not case["matches"] for case in controls + reproductions) and checks["experiment"]["status"] == "assessed":
        blockers = [i for i in issues if i["domain"] == "experiment" and i["severity"] == "blocker"
                    and i["status"] in ("new", "still_present")]
        if not blockers:
            raise ValueError("The worker and reviewer disagreed. Check both calculations against the original "
                             "definition. Locate a supported submission defect as an experiment blocker, or "
                             "mark experiment unassessed and explain the discrepancy.")
        if any(not case["matches"] for case in reproductions):
            method = snapshot["measurement_method"]
            sources = {task["artifact"], method["artifact"]}
            if method.get("request"):
                sources.add(method["request"])
            if not any(sources <= {r["artifact"] for r in i["evidence"]}
                       and not _delegated_calculation(snapshot, method["artifact"], i["evidence"]) for i in blockers):
                raise ValueError("The reproduced row differs. In the finding that attributes the defect to "
                                 "the submission, cite the measured row and the executed operation against "
                                 "its requested definition. Use the observed passages from "
                                 "checks.experiment.evidence. If the cause is unresolved, mark experiment "
                                 "unassessed and explain the two observations.")
    if any(i["severity"] == "blocker" and i["status"] in ("new", "still_present") for i in issues):
        verdict = "revise"
    elif any(checks[d]["status"] == "unassessed" for d in snapshot["required"]) or any(
            i["severity"] == "blocker" and i["status"] == "unassessed" for i in issues):
        verdict = "unassessed"
    else:
        verdict = "accept"
    submitted = response.get("verdict")
    explicit_missing_block = submitted == "revise" and verdict == "unassessed" and missing_observations
    if submitted != verdict and not explicit_missing_block:
        raise ValueError(f"The proposed verdict disagrees with the open findings or missing evidence: expected {verdict}")
    return {"version": REVIEW_VERSION, "revision": snapshot["revision"], "verdict": verdict,
            "submitted_verdict": submitted, "deliverable": verdict == "accept", "summary": summary, "alternative": alternative,
            "artifact_hashes": {p: m["sha256"] for p, m in snapshot["artifacts"].items()},
            "closed_ids": sorted(closed_ids | {i["id"] for i in issues if i["status"] == "resolved"}),
            "strengths": strengths, "checks": checks, "issues": issues, "score": score}


def record_is_current(snapshot, record):
    if not isinstance(record, dict) or record.get("version") != REVIEW_VERSION or record.get("revision") != snapshot["revision"]:
        return False
    if snapshot.get("evidence_scope"):
        scope = record.get("evidence_scope")
        if not isinstance(scope, dict) or not all(isinstance(scope.get(key), str) and scope[key].strip()
                for key in ("comparison", "alternative", "discriminator")):
            return False
    if snapshot.get("figure_methods") and record.get("verdict") == "accept":
        origins = (record.get("checks", {}).get("visual", {}).get("series_origin") or {})
        return set(origins) == set(snapshot["figure_methods"]) and all(
            value in ("measured", "theoretical") for value in origins.values())
    return True


def main():
    try:
        body = json.load(sys.stdin)
        op = sys.argv[1]
        if op == "snapshot":
            result = build_snapshot(**body)
        elif op == "evidence":
            result = read_evidence(**body)
        elif op == "prompt":
            result = {"prompt": review_prompt(**body)}
        elif op == "validate":
            result = validate_review(**body)
        else:
            raise ValueError("Expected snapshot, evidence, prompt or validate")
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
