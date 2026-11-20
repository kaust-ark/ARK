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
                   registered_plots=None, measurement_run=None, expected_measurement=None, measurement_comparison=None,
                   experiment_plan=None):
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
        if bound and experiment_plan and experiment_plan.get("expected") == expected_measurement:
            from ark.compute.native_experiment import plan_digest
            from ark.measurement_table import experiment_design
            if run.get("plan_sha256") == plan_digest(experiment_plan):
                data = None
                if (experiment_plan.get("mechanism") or {}).get("measurement"):
                    payload, _ = _artifact({**manifest, "project": str(root)}, expected_measurement)
                    data = json.loads(payload)
                manifest["registered_design"] = experiment_design(experiment_plan, data)
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
                    recheck = {"version":3}
                    if manifest.get("registered_design"):
                        recheck["plan_sha256"] = run["plan_sha256"]
                    scope = {"version": 4, "tables": tables[:8], "recheck_paragraphs":recheck}
                    if conclusion and (clauses := _conclusion_clauses(conclusion)):
                        scope["conclusion_clauses"] = clauses
                    passages = _explanatory_passages({**manifest, "project": str(root)}, pdf)
                    columns = {col.casefold() for table in tables[:8] for col in table["columns"]}
                    scope.update({k: v for k, v in passages.items() if k not in NUMERIC_SELECTORS
                                  or v["assertion"]["axis"].casefold() in columns})
                    manifest["evidence_scope"] = scope
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


def _conclusion_sentences(text):
    return re.split(r'(?<=[.!?])\s+(?=[A-Z“"‘])', text.strip())


def _conclusion_clauses(conclusion):
    """Locate mixed outcome/explanation sentences. The reviewer judges the relation."""
    result_verb = re.compile(r"\b(?:improv|degrad|reduc|increas|decreas|declin|worsen|enhanc|accelerat)\w*\b", re.I)
    connector = re.compile(r"\b(?:because(?: of)?|owing to|due to|as a result of|through)\b", re.I)
    temporal = re.compile(r"\s+(?:(?:the|this)\s+)?(?:\d|end\b|follow.up\b|time\b|day\b|week\b|month\b|year\b|epoch\b|round\b)", re.I)
    clauses = []
    for n, sentence in enumerate(_conclusion_sentences(conclusion["quote"]), 1):
        for link in connector.finditer(sentence):
            effect = sentence[:link.start()].rstrip(" ,")
            if not effect or (link[0].lower() == "through" and (
                    not result_verb.search(effect) or temporal.match(sentence[link.end():]))):
                continue
            clauses.append({"sentence":n, "result":effect, "continuation":sentence[link.start():]})
            break
        if len(clauses) == 2:
            break
    return clauses


# Keep bounded locators for separate comparative claims across the rendered body.
SUPERIORITY_SELECTORS = ("superiority", "superiority_2", "superiority_3")
NUMERIC_SELECTORS = ("fractional_change", "ordered_change")
COMPARISON_SELECTORS = ("contribution", "direction", *SUPERIORITY_SELECTORS, *NUMERIC_SELECTORS)
_FRACTIONAL_ASSERTION = re.compile(
    r"\b(?:remov(?:e[sd]?|ing)|reduc(?:e[sd]?|ing)|eliminat(?:e[sd]?|ing)|cuts?)\s+"
    r"(?:[^.;!?]{1,50}?\bby\s+)?(?P<relation>more than|less than|at least|at most|over|under)\s+"
    r"(?P<amount>(?:one[- ]|a )?half|(?:\d+(?:\.\d+)?)\s*%)", re.I)
_ORDERED_ASSERTION = re.compile(
    r"\b(?P<subject>correction(?: term)?|difference|gap|gain|contrast|advantage|effect(?: size)?)\s+"
    r"(?P<direction>increases?|decreases?|rises?|falls?|grows?|shrinks?)\s+with\s+(?P<axis>[A-Za-z]\w*)\b", re.I)


def _assertion_text(quote):
    # A typeset line can split a word in the middle of the comparator.
    return _normalized_quote(re.sub(r"(?<=[A-Za-z])-\s*\n(?=[a-z])", "", quote))


def _condition_assertions(quote):
    text = _assertion_text(quote)
    if match := _FRACTIONAL_ASSERTION.search(text):
        conditions = re.findall(r"(?<!\w)([A-Za-z]\w*)\s*=\s*([-+]?\d+(?:\.\d+)?)", text)
        if conditions and len({key for key, val in conditions}) == 1:
            amount = match["amount"].strip()
            yield "fractional_change", {"axis":conditions[0][0], "levels":[float(v) for _, v in conditions],
                "relation":match["relation"].lower(), "threshold":float(amount[:-1])/100 if amount.endswith("%") else .5}
    if match := _ORDERED_ASSERTION.search(text):
        yield "ordered_change", {"axis":match["axis"], "subject":match["subject"].lower().split()[0], "increasing":match["direction"].lower().startswith(
            ("increase","rise","grow"))}

_SUPERIORITY_PATTERNS = (
    r"\b(?:more|less)\s+(?:\w+[ -]){0,2}(?:robust|accurate|efficient|effective|reliable|stable|precise|sensitive|biased|variable|consistent)\s+(?:\w+\s+){0,3}than\b",
    r"\b(?:lower|higher|smaller|greater)\s+(?:(?-i:[A-Z]{2,8})|error|accuracy|variance|latency|cost|loss)\s+than\b",
    r"\b(?:outperform(?:s|ed|ing)?|surpass(?:es|ed|ing)?|superior to|inferior to)\b",
    r"\b(?:better|worse|faster|slower|lower|higher|smaller|larger|fewer|greater)\b(?:(?!\brather than\b)[^.!?;]){0,85}(?<!rather )\bthan\b",
)


def _explanatory_passages(snapshot, pdf):
    """Locate comparative assertions in the rendered body and expose their exact page windows."""
    import fitz
    from ark.latex.utils import body_page_boundary

    patterns = {
        "attribution": (
            r"\b(?:dominated by|dominant (?:cause|mechanism|contribution)|primarily (?:driven|caused|explained) by)\b",
            r"\b(?:mediated by|attributable to|explained by|caused by|driven by)\b",
        ),
        "contribution": (
            r"\boutweigh(?:s|ed)?\b",
            r"\b(?:accounts? for (?:more|less)|contributes? (?:more|less))\b.{1,100}\bthan\b",
        ),
        "direction": (
            r"\b(?:closer to|farther from|further from)\b.{1,100}\bthan\b",
            r"\b(?:shift(?:s|ed)?|moves?|moved)\b.{0,60}\btowards?\b",
        ),
    }
    best = {}
    superiority = {}
    data, _ = _artifact(snapshot, pdf)
    try:
        document = fitz.open(stream=data, filetype="pdf")
    except (RuntimeError, ValueError):
        return {}
    with document as doc:
        boundary = body_page_boundary(doc)
        end = min(len(doc), boundary[0] + int(any(boundary[1]))) if boundary else len(doc)
        for page in range(end):
            text = doc[page].get_text()
            for block in doc[page].get_text("blocks"):
                if block[6] != 0:
                    continue
                for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", block[4].strip()):
                    if not 12 <= len(sentence) <= 1200 or not sentence.endswith((".", "!", "?")):
                        continue
                    offset = text.find(sentence)
                    if offset < 0:
                        continue
                    words = _normalized_quote(sentence)
                    for selector, assertion in _condition_assertions(sentence):
                        best.setdefault(selector, (0, {"artifact":pdf, "page":page + 1, "start":offset + 1,
                                                       "quote":sentence,"assertion":assertion}))
                    for rank, pattern in enumerate(_SUPERIORITY_PATTERNS):
                        if re.search(pattern, words, re.I):
                            # Prefer explicit empirical assertions when several passages make the same comparison.
                            empirical = bool(re.search(r"\b(?:our|we|results?|measurements?|experiments?)\b", words, re.I))
                            superiority.setdefault(words, ((rank, not empirical, -page, offset),
                                {"artifact":pdf, "page":page + 1, "start":offset + 1, "quote":sentence}))
                            break
                    for selector, options in patterns.items():
                        for rank, pattern in enumerate(options):
                            if (selector not in best or rank < best[selector][0]) and re.search(pattern, words, re.I):
                                best[selector] = (rank, {"artifact":pdf, "page":page + 1, "start":offset + 1, "quote":sentence})
                                break
    best.update(zip(SUPERIORITY_SELECTORS, sorted(superiority.values(), key=lambda item: item[0])))
    return {selector: passage for selector, (_, passage) in best.items()}


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


def _visible_measurement_rows(text):
    """Return the exact complete row objects at the start of an observed JSON array."""
    pos = len(text) - len(text.lstrip())
    if text[pos:pos + 1] != "[":
        return []
    rows = []
    decoder = json.JSONDecoder()
    for _ in range(64):
        pos += 1  # Opening array or comma after the preceding row.
        pos += len(text[pos:]) - len(text[pos:].lstrip())
        if text[pos:pos + 1] != "{":
            break
        try:
            _, end = decoder.raw_decode(text, pos)
        except (ValueError, RecursionError):
            break
        if end - pos > 1200:  # Keep row references within the regular quotation limit.
            break
        rows.append(text[pos:end])
        pos = end + len(text[end:]) - len(text[end:].lstrip())
        if text[pos:pos + 1] != ",":
            break
    return rows


def _primary_record(text):
    try:
        record = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return record if isinstance(record, dict) and isinstance(record.get("passage"), str) else None


def read_evidence(snapshot, artifact, *, page=None, start=1, visual=False, statistic=None, column="", rows_pointer="",
                  find=None, reference=None, condition_columns=None):
    """Return a bounded text excerpt or an actual rendered PDF page with a receipt."""
    if type(start) is not int or start < 1 or type(visual) is not bool:
        raise ValueError("Invalid evidence offset or mode")
    data, meta = _artifact(snapshot, artifact)
    ext = Path(artifact).suffix.lower()
    receipt = {"revision": snapshot["revision"], "artifact": artifact, "sha256": meta["sha256"], "start": start}
    result = {"artifact": artifact, "sha256": meta["sha256"]}
    if find is not None and ((ext != ".pdf" and meta["kind"] != "primary_source") or visual or statistic is not None or start != 1
                             or not isinstance(find, str) or len(find.strip()) < 2 or len(find) > 160):
        raise ValueError("Find a phrase of 2 to 160 characters in a PDF or primary source using text mode. A PDF can include a starting page.")
    if statistic == "paired":
        from ark.measurement_table import summarize
        if (meta["kind"] != "measurement" or ext != ".json" or visual or page is not None
                or not reference or not condition_columns or len(data) > 4 * 1024 * 1024):
            raise ValueError("For a paired comparison select a measured JSON table under 4 MiB, a column, "
                             "a reference and condition_columns.")
        summary = summarize(json.loads(data), rows_pointer, column, reference, condition_columns)
        text = "\n".join(json.dumps(row, ensure_ascii=False) for row in summary["per_condition"])
        chunk, end = _text_window(text, start)
        if start > 1 and text[start - 2] != "\n":
            raise ValueError("Use next_start to read the next complete paired comparison.")
        next_start = None
        if end < len(text):
            end = text.rindex("\n", start - 1, end + 1)
            chunk = text[start - 1:end]
            next_start = end + 2
        receipt.update(statistic=statistic, column=column, reference=reference, condition_columns=condition_columns,
                       rows_pointer=rows_pointer, end=end)
        return {**result, "text": chunk, "next_start": next_start, "receipt": receipt}
    if reference is not None or condition_columns is not None:
        raise ValueError("Select statistic=paired for a condition-specific comparison.")
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
            if find is not None:
                # Locations on other pages are navigation hints. Only the returned
                # text window earns the existing revision- and page-bound receipt.
                begin = _page_index(page, len(doc))
                end = min(len(doc), begin + 32)
                phrase = re.compile(r"\s+".join(map(re.escape, find.split())), re.I)
                matches = []
                for idx in range(begin, end):
                    match = phrase.search(doc[idx].get_text())
                    if match:
                        matches.append({"page": idx + 1, "start": max(1, match.start() - 199)})
                result.update(matching_pages=matches, next_search_page=end + 1 if end < len(doc) else None)
                if not matches:
                    return {**result, "text": ""}
                page, start = matches[0]["page"], matches[0]["start"]
                receipt["start"] = start
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
            cursor = 0
            blocks = []
            for number, block in enumerate(pg.get_text("blocks"), 1):
                if block[6] != 0:
                    continue
                segment = block[4].strip()
                offset = text.find(segment, cursor)
                if offset < 0 or not segment:
                    continue
                cursor = offset + len(segment)
                if start - 1 <= offset and cursor <= start - 1 + VIEW_SIZE and 12 <= len(segment) <= 1200:
                    blocks.append({"block":number, "start":offset + 1, "length":len(segment),
                                   "begins":" ".join(segment.split())[:64]})
            if blocks:
                result["citable_blocks"] = blocks
    else:
        if visual or page is not None or ext in {".png", ".svg", ".jpg", ".jpeg"}:
            raise ValueError("Inspect figures on their rendered manuscript page")
        text = data.decode("utf-8", errors="replace")
        if meta["kind"] == "primary_source":
            source = _primary_record(text)
            if source:
                text = "\n\n".join(source[key] for key in ("source", "title", "url", "passage")
                                   if isinstance(source.get(key), str) and source[key])
            if find:
                match = re.search(r"\s+".join(map(re.escape, find.split())), text, re.I)
                if not match:
                    return {**result, "text":""}
                start = receipt["start"] = max(1, match.start() - 199)
    chunk, receipt["end"] = _text_window(text, start)
    if start == 1 and ext == ".json" and meta["kind"] == "measurement":
        rows = _visible_measurement_rows(chunk)
        if rows:
            result["citable_rows"] = len(rows)
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


def claim_instructions(snapshot):
    scope = snapshot["evidence_scope"]
    requests = []
    for clause in scope.get("conclusion_clauses", ()):
        passage = {"selector":"conclusion", "sentence":clause["sentence"]}
        requests.extend(({"kind":"result","passage":passage}, {"kind":"explanation","passage":passage}))
    for selector in ("attribution", *COMPARISON_SELECTORS):
        if scope.get(selector):
            requests.append({"kind":"result" if selector in (*SUPERIORITY_SELECTORS, *NUMERIC_SELECTORS) else "explanation","passage":{"selector":selector}})
    parts = [
        "Return checks.manuscript with status, reason and claims. For each claim supply kind, passage, basis, "
        "evidence and reason. Give the result its own assessment using an observed worker row. "
        "A clearly descriptive paper needs only its result.",
        "Check claims of a direction, threshold or relative change at every condition they name. Choose the "
        "matching columns and denominator. For paired measured JSON fields, read_evidence(statistic=paired, "
        "column=..., reference=..., condition_columns=[...]) returns a signed difference and fraction reduction "
        "in magnitude for each row. Cite the complete returned JSON line for each disputed condition. Use "
        "unrounded values for strict inequalities and distinguish a change in signed value from a change in magnitude.",
    ]
    if snapshot.get("conclusion"):
        parts.append("Use the conclusion sentence selector or an observed PDF passage.")
    else:
        parts.append("Select the paper's principal empirical claim from the rendered abstract, results or discussion. "
                     "Open its page and use the actual PDF passage with artifact, page and quote. Check any "
                     "substantive explanation separately. A section heading is optional.")
    if requests:
        parts.append("Assess each listed passage. These are suggested kinds. Use result for measured comparisons or "
                     "explanation for analytic, qualified or unsupported inferences. Always use its printed passage selector. " + json.dumps(requests, separators=(",", ":")))
    parts.extend((
        "A measurement can establish an outcome. For an explanation choose control when an observed method "
        "and measured contrast distinguish the mechanism, or derivation when observed reasoning reaches the "
        "claimed outcome (quote that step in bridge). For qualified, quote the author's actual limiting words "
        "in qualification. The located passage itself supplies the citation for qualified. For a claim that "
        "one factor explains more of an effect than another, look for a comparison of their contributions. "
        "If their ordering is still open, check whether the authors present that ordering as a hypothesis. "
        "Only use procedure for a conclusion "
        "through-clause that literally describes a documented experimental operation.",
        'When the paper presents an explanation as established and the evidence falls short, choose '
        'basis="unsupported", cite what was actually available, explain the gap in reason, and give '
        'finding={"id":"R1","fix":"the smallest repair"} on that claim. This records an open manuscript '
        'blocker using that exact passage and evidence. Return verdict="revise" when an open blocker remains. '
        'Use its earlier ID if it persists. List other '
        'findings in issues. A first review cannot resolve a finding, and advice to change the paper '
        'does not resolve it. A disputed finding on a different page cannot cover stronger wording here.',
        'For plain PDF evidence give artifact, page and a verbatim quote. A page number alone cannot identify '
        'words. A selector already identifies a passage. For typeset equations cite an observed PDF paragraph '
        'as {"artifact":"paper/main.pdf","page":N,"block":B} using its citable_blocks number. If that '
        'paragraph derives the claim, set bridge={"evidence":K}, the one-based position of that reference '
        'in this claim’s evidence array. Explain why the calculation establishes the claim in reason.',
    ))
    if any(scope.get(selector) for selector in COMPARISON_SELECTORS):
        parts.append("For a contribution ordering or a directional comparison, identify the quantities compared. "
                     "If you use control, give measured_columns listing the actual worker fields cited in your "
                     "observed rows or statistics. Explain how the measurements distinguish the stated relation. "
                     "A direct descriptive comparison may use kind=result and basis=measurement with measured_columns. "
                     "An analytic implication may use derivation; a stated hypothesis may use qualified. "
                     "If only an aggregate outcome or a proxy is available, identify the untested step.")
    if any(scope.get(key) for key in NUMERIC_SELECTORS):
        parts.append('For a fractional_change or ordered_change result, open read_evidence with statistic="paired", '
                     'choosing the reference and comparison in the paper and the named condition in condition_columns. '
                     'Cite a complete calculated row, as printed. Give quantity="fraction_reduction_in_magnitude" '
                     'for a fractional reduction or quantity="signed_difference" for the trend of a correction or gap. '
                     'If the worker reports the exact change in one numeric field, cite that row, give recorded_column and '
                     'unit (fraction, percent or value) plus rows_pointer for a nested table. Explain the column mapping. '
                     'Check the entire stated scope. A counterexample '
                     'requires an unsupported finding unless the paper explicitly qualifies the claim. For the theoretical '
                     'rather than measured meaning, cite a separate derivation and its assumptions.')
    if any(scope.get(selector) for selector in SUPERIORITY_SELECTORS):
        parts.append('For a superiority claim, identify precisely what the paper compares. For measurement or '
                     'control include comparison=[{"subject":"words from the passage","column":"actual field"},'
                     '{"subject":"words for the other side","column":"actual field"}]. A long table can use '
                     'the same outcome column with condition={"column":"arm","value":"exact row value"} on each '
                     'side. Cite complete observed rows for conditioned values. For a single paired contrast '
                     'give each side its worker operand in operand, and quote the sealed worker calculation '
                     'in contrast_expression and evidence. In reason explain what the fields and metric measure. '
                     'Check both operands against the study method. A column for a different comparator cannot '
                     'stand in for the one in the paper. If a comparator was never measured, use unsupported and '
                     'give a finding, unless the text explicitly qualifies it as a hypothesis or an independent '
                     'derivation establishes it. For a post hoc result computed from sealed measurements, cite the '
                     'original inputs, independent calculation and appendix or supporting table. Use explanation '
                     'with derivation and quote the calculation in bridge. Distinguish it from a preregistered arm.')
    if snapshot.get("method_control"):
        parts.append("If your derivation relies on the submission's own reasoning, independently check its "
                     "implication at the disputed passage. Use test_inference on two admissible cases and "
                     "compare the claimed outcome. A matching example alone does not prove an implication. "
                     "For an analytic argument, supply independent_derivation with your own calculation from "
                     "the assumptions to the claimed quantity. If the calculation stops at an intermediate, "
                     "reconsider the basis. If the study excludes a counterexample, cite the restriction and "
                     "explain it in counterexample_limit.")
        if _needs_inference_test(snapshot):
            parts.append("The executed plan has no complete registered mechanism measurement. If a mixed "
                         "conclusion attributes the observed change to a mechanism using a derivation solely "
                         "from this manuscript, test the implication with test_inference at that passage. "
                         "Keep the premise constant across different cases and compare the claimed endpoint. "
                         "You can instead cite an observed discriminating control, a relevant independent "
                         "derivation, identify genuinely limited wording, or raise the missing observation. "
                         "A calculation of the premise alone cannot settle the question. "
                         "If the study excludes a counterexample, give exclusion_evidence referencing the observed "
                         "protocol, measurement, source or method with that restriction.")
    return "\n".join(parts)


def review_prompt(snapshot, previous=None):
    instructions = (Path(__file__).parent / "templates/agents/native_reviewer.prompt").read_text()
    tex, pdf = _submission(snapshot)
    catalogue = {p: v["kind"] for p, v in snapshot["artifacts"].items()
                 if Path(p).suffix.lower() not in {".sty", ".bst", ".cls"}}
    packet = {"revision": snapshot["revision"], "target_track": snapshot["target_track"] or "unspecified",
              "required": snapshot["required"], "manuscript_artifact": tex, "rendered_artifact": pdf,
              "manuscript_excerpt": _blind_text(snapshot), "available_artifacts": catalogue}
    if design := snapshot.get("registered_design"):
        packet["executed_experiment_design"] = design
        instructions += ("\nCompare your pre-reading judgment with the registered design and the actual worker "
                         "observations. Registered columns describe coverage. Inspect their values and methods "
                         "to decide what they establish. Later controls or an independent proof can add evidence.")
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
            "test or narrower wording that would resolve it. Assess this conclusion paragraph on its own terms. "
            "If a different section makes an unsupported assertion, locate a separate finding on that page even "
            "when the conclusion is appropriately qualified. Mark manuscript unassessed if the evidence is unavailable.")
    if snapshot.get("evidence_scope", {}).get("version") in (3, 4):
        # The scoring contract is delivered separately from the untrusted manuscript.
        packet.pop("conclusion_assessment", None)
        for selector in ("attribution", *COMPARISON_SELECTORS):
            if passage := snapshot["evidence_scope"].get(selector):
                packet[selector + "_to_check"] = passage
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
    tables = snapshot.get("evidence_scope", {}).get("tables")
    if tables and snapshot.get("method_control") and (method := snapshot.get("measurement_method", {}).get("artifact")):
        from ark.compute.review_control import result_column_targets
        if targets := result_column_targets(_artifact(snapshot, method)[0], tables):
            packet["result_column_targets"] = targets
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
        contexts = revision_paragraphs(snapshot, previous)
        if contexts:
            packet["revised_paragraphs"] = {
                key: {k:v for k,v in context.items() if k not in ("paragraph", "surviving")} | {"end":context["start"] + len(context["paragraph"]) - 1}
                for key, context in contexts.items()
            }
            instructions += ("\nRead the complete revised paragraph for each linked blocker. Compare the remaining "
                             "numbered sentences (one-based, in the remaining array) with the original evidence gap. "
                             "Where remaining_locations is provided, use its matching page and text window for each sentence. "
                             "If any still asserts the unsupported inference, keep the blocker still_present and cite "
                             "the surviving sentence as the issue claim with current PDF evidence. When the old claim changed "
                             "and you dispute its replacement, compare the changed words in issue.revision_comparison "
                             "with previous, current and reason. Quote words specific to each version and explain why the "
                             "change still leaves an unsupported implication. Give the full current sentence in claim "
                             "and cite the relevant observation. Newly changed evidence may also establish a current defect. "
                             "If you cannot locate one, assess resolution or mark it unassessed. "
                             "To resolve it, give the issue a context_assessment array with one "
                             "entry per remaining sentence: {sentence, judgment, reason}. Use limited with qualification "
                             "quoting the author's limiting words at that inference. Use measured with quantity (the "
                             "printed metric name), measured_columns and this entry's own evidence citing those worker "
                             "fields or computed statistics. For a metric alias cite its definition in the protocol or code. Use documented with this entry's own evidence if the "
                             "original source directly states the sentence, or the registered design records a neutral "
                             "procedure in its baseline, comparison or controls. For a procedural rewording, supply the exact registered operation and cite its executed code. For an analytic implication use "
                             "derived with claim_ref pointing to a checked derivation of that exact sentence in "
                             "checks.manuscript.claims. A different topic or the reviewer's conceptual explanation "
                             "cannot establish what the author asserts. If no basis applies, keep the finding open "
                             "or mark it unassessed and cite the unresolved sentence.")
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


def _observed_conclusion(snapshot, observed, selector="conclusion"):
    target = (snapshot.get("conclusion") if selector == "conclusion"
              else snapshot.get("evidence_scope", {}).get(selector))
    if not target:
        raise ValueError(f"This snapshot has no {selector} selector.")
    artifact, quote = target["artifact"], target["quote"]
    for page, start, window in _observed_pdf_windows(snapshot, artifact, observed):
        offset = target["start"] - start
        if page == target["page"] and offset >= 0 and window[offset:offset + len(quote)] == quote:
            return {"artifact": artifact, "page": page, "quote": quote,
                    "sha256": snapshot["artifacts"][artifact]["sha256"]}
    raise ValueError(f'The {selector} selector needs an observed text window from this revision. '
                     f'Read {artifact}, page {target["page"]}, start {target["start"]}, visual=false.')


def _references(snapshot, refs, observed, blind, *, required, pdf_windows=None):
    if not isinstance(refs, list) or len(refs) > 24 or (required and not refs):
        raise ValueError("Provide specific evidence for this judgment")
    tex, _ = _submission(snapshot)
    verified = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise ValueError("Expected an evidence reference")
        if "row" in ref:
            if ref.keys() != {"artifact", "row"} or not isinstance(ref["artifact"], str) or type(ref["row"]) is not int:
                raise ValueError("For a measurement row supply only its artifact and one-based row number.")
            view = read_evidence(snapshot, ref["artifact"])
            if view["receipt"] not in observed or not 1 <= ref["row"] <= view.get("citable_rows", 0):
                raise ValueError("Cite a complete measured row actually observed in this review.")
            row = _visible_measurement_rows(view["text"])[ref["row"] - 1]
            ref = {"artifact": ref["artifact"], "quote": row}
        if "selector" in ref:
            sentence = ref.get("sentence")
            if (ref["selector"] not in ("conclusion", "attribution", *COMPARISON_SELECTORS) or ref.keys() - {"selector", "artifact", "page", "sentence"}
                    or ("sentence" in ref and type(sentence) is not int)):
                raise ValueError('Use only a supplied passage selector with optional artifact, page and one-based sentence number.')
            located = _observed_conclusion(snapshot, observed, ref["selector"])
            if ("artifact" in ref and ref["artifact"] != located["artifact"]) or (
                    "page" in ref and (type(ref["page"]) is not int or ref["page"] != located["page"])):
                raise ValueError("The supplied passage locator conflicts with the observed PDF artifact or page.")
            if sentence is not None:
                parts = _conclusion_sentences(located["quote"])
                if not 1 <= sentence <= len(parts):
                    raise ValueError(f"Select a {ref['selector']} sentence between 1 and {len(parts)}.")
                located = {**located, "quote":parts[sentence - 1]}
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
        if "block" in ref:
            if ref.keys() != {"artifact", "page", "block"} or type(ref["block"]) is not int or not page:
                raise ValueError("Use artifact, page and the printed one-based PDF text block number.")
            for receipt in receipt_matches:
                view = read_evidence(snapshot, artifact, page=page, start=receipt.get("start", 1))
                if view["receipt"] != receipt:
                    continue
                block = next((r for r in view.get("citable_blocks", ()) if r["block"] == ref["block"]), None)
                if block:
                    pos = block["start"] - receipt["start"]
                    verified.append({"artifact":artifact, "page":page, "sha256":info["sha256"],
                                     "quote":view["text"][pos:pos + block["length"]]})
                    break
            else:
                raise ValueError("Read this PDF page in text mode and cite a complete block listed in citable_blocks.")
            continue
        if visual:
            if not receipt_matches or not any(r.get("image_sha256") for r in receipt_matches):
                raise ValueError("The reviewer did not observe pixels of the cited page")
            verified.append({"artifact": artifact, "page": page, "visual": True, "sha256": info["sha256"],
                             "image_sha256": receipt_matches[0]["image_sha256"]})
            continue
        # A complete computed row can contain several long field names or labels.
        # Only an exact line from the paired view can support this longer citation.
        quote_limit = 1200
        if isinstance(ref.get("quote"), str) and len(ref["quote"]) > quote_limit:
            receipt_matches = [r for r in receipt_matches if r.get("statistic") == "paired"]
            if receipt_matches:
                quote_limit = VIEW_SIZE
        quote = _string(ref.get("quote"), "verbatim evidence quote", limit=quote_limit)
        if len(quote) < 4:
            raise ValueError("Evidence quote is too short to locate")
        located = artifact == tex and _contains_observed_quote(blind, quote)
        calculation = {}
        for r in receipt_matches:
            if located:
                break
            options = {k: r[k] for k in ("statistic", "column", "rows_pointer", "reference", "condition_columns") if k in r}
            view = read_evidence(snapshot, artifact, page=page, start=r.get("start", 1), **options)
            if view["receipt"] != r:
                continue
            if options.get("statistic") == "paired":
                tokens = _json_container_tokens(quote)
                located = bool(tokens) and any(tokens == tuple(_JSON_TOKEN.findall(line))
                                               for line in view["text"].splitlines())
            elif Path(artifact).suffix.lower() == ".json" and not options:
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


def _issue_history_problem(issue, prior, closed):
    ident, status = issue.get("id"), issue.get("status")
    if not isinstance(ident, str):
        return None  # ID syntax is checked separately.
    if ident in closed:
        return f"{ident} has invalid revision history. Choose a new ID for a new concern."
    if ident not in prior and status not in ("new", "unassessed"):
        return f"{ident} has invalid revision history. A first report is new or unassessed. A proposed fix has not been performed."
    if ident in prior and status == "new":
        return f"{ident} has invalid revision history. Recheck the earlier finding before assigning its status."
    return None


def draft_evidence_feedback(snapshot, proposal, observed, first_error, previous=None):
    """Report independent evidence repairs together, without altering any proposed finding."""
    if not isinstance(proposal, dict):
        return first_error
    checks, issues = proposal.get("checks"), proposal.get("issues")
    if not isinstance(checks, dict) or not isinstance(issues, list):
        return first_error
    blind = _blind_text(snapshot)
    repairs = [first_error]
    manuscript = checks.get("manuscript")
    per_claim = snapshot.get("evidence_scope", {}).get("version") in (3, 4)
    if per_claim and isinstance(manuscript, dict) and manuscript.get("status") == "assessed" and not manuscript.get("claims"):
        repairs.append(claim_instructions(snapshot))
    if (not per_claim and snapshot.get("conclusion") and isinstance(manuscript, dict) and manuscript.get("status") == "assessed"
            and manuscript.get("conclusion_support") not in ("measured", "interpretation", "needs_evidence")
            and "conclusion_support" not in first_error):
        repairs.append("checks.manuscript.conclusion_support is missing or invalid. Assess the whole conclusion "
                       "as measured, interpretation or needs_evidence, including any inference beyond the outcome.")
    pdf_windows = {}
    # Report independent provenance problems on the first repair turn, even
    # when the manuscript judgment itself still needs work.
    for domain in ("experiment", "citations"):
        check = checks.get(domain)
        if not isinstance(check, dict) or check.get("status") != "assessed":
            continue
        try:
            refs = _references(snapshot, check.get("evidence", []), observed, blind, required=True, pdf_windows=pdf_windows)
            _check_source_and_method(snapshot, domain, refs)
        except ValueError as exc:
            if str(exc) != first_error:
                repairs.append(f"checks.{domain}: {exc}")
    if per_claim and isinstance(manuscript, dict) and isinstance(manuscript.get("claims"), list):
        located = []
        for index, claim in enumerate(manuscript["claims"][:12], 1):
            if not isinstance(claim, dict):
                continue
            label = f"Manuscript claim {index}"
            try:
                passage = reviewed_claim_passage(snapshot, claim.get("passage"), observed)
            except ValueError as exc:
                repairs.append(f'{label} passage: {exc} For the located attribution use {{"selector":"attribution"}}; '
                               'for a conclusion sentence use {"selector":"conclusion","sentence":1} with the actual number.')
            else:
                if claim.get("kind") == "explanation":
                    located.append(passage)
                if claim.get("basis") == "procedure" and not _procedure_passage(snapshot, passage):
                    repairs.append(f"{label} basis: procedure describes how an experiment was performed. "
                                   "Judge the proposed explanation against its own evidence.")
                if "evidence" in claim or claim.get("basis") != "qualified":
                    try:
                        _references(snapshot, claim.get("evidence"), observed, blind, required=True, pdf_windows=pdf_windows)
                    except ValueError as exc:
                        repairs.append(f"{label} evidence: {exc}. A PDF text reference needs its verbatim quote.")
                        continue
                try:
                    _manuscript_claims(snapshot, [claim], observed, blind, require_coverage=False)
                except ValueError as exc:
                    ref = claim.get("passage")
                    selector = ref.get("selector", "quoted passage") if isinstance(ref, dict) else "quoted passage"
                    repairs.append(f"{label} ({selector}): {exc}")

        for parts in snapshot["evidence_scope"].get("conclusion_clauses", ()):
            if not any(p["page"] == snapshot["conclusion"]["page"] and _contains_observed_quote(
                    p["quote"], parts["continuation"]) for p in located):
                repairs.append(f'Assess the explanation on PDF page {snapshot["conclusion"]["page"]} using '
                               f'kind=explanation and passage={{"selector":"conclusion","sentence":{parts["sentence"]}}}.')
    prior = {i["id"]: i for i in previous["issues"]} if previous else {}
    closed = set(previous.get("closed_ids", [])) if previous else set()
    for issue in issues[:8]:
        if isinstance(issue, dict) and (problem := _issue_history_problem(issue, prior, closed)):
            repairs.append(problem)
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
    return "\n".join(dict.fromkeys(repairs))


def _check_source_and_method(snapshot, domain, refs):
    if domain == "citations":
        for ref in refs:
            if snapshot["artifacts"][ref["artifact"]]["kind"] != "primary_source" or ref.get("visual"):
                continue
            if Path(ref["artifact"]).suffix.lower() not in (".md", ".txt"):
                continue
            data, _ = _artifact(snapshot, ref["artifact"])
            source = _primary_record(data.decode("utf-8", errors="replace"))
            if source:
                quote = _normalized_quote(ref.get("quote", "")).casefold()
                if (not quote or quote in _normalized_quote(source.get("title") or "").casefold()
                        or quote not in _normalized_quote(source["passage"]).casefold()):
                    raise ValueError(f"Quote the authors' scientific statement from {ref['artifact']}. "
                                     "The title identifies the publication but supplies no evidence for the claim. "
                                     "Use read_evidence with find to open a relevant passage.")
    elif domain == "experiment" and (method := snapshot.get("measurement_method")):
        paths = {r["artifact"] for r in refs}
        if not method["artifact"]:
            raise ValueError("The worker's executed source is unavailable. Mark experiment unassessed.")
        if method["artifact"] not in paths or (method.get("request") and method["request"] not in paths):
            raise ValueError("Trace the measured quantity to its requested definition, executed calculation "
                             "and recorded rows in checks.experiment.evidence.")
        if delegated := _delegated_calculation(snapshot, method["artifact"], refs):
            raise ValueError(f"The quoted code delegates the calculation to {delegated}. Inspect that "
                             "definition and quote the operation that produces the measured quantity.")


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


def _same_open_finding(issue, claim, status):
    """Match two reports of one verified blocker, including the exact observed evidence."""
    if (issue["domain"], issue["severity"], issue["status"]) != ("manuscript", "blocker", status):
        return False
    passage = claim["passage"]
    # An explicit finding may remove a discretionary hyphen from the rendered page.
    prose = re.sub(r"(?<=[^\W\d_])-\n(?=[^\W\d_])", "", passage["quote"])
    if _normalized_quote(issue["claim"]) not in {_normalized_quote(passage["quote"]), _normalized_quote(prose)}:
        return False
    def cited(ref):
        if "quote" in ref:
            ref = {**ref, "quote": _normalized_quote(ref["quote"])}
        return _canonical(ref)
    listed = {cited(ref) for ref in issue["evidence"]}
    attached = {cited(ref) for ref in [passage, *claim["evidence"]]}
    if listed == attached:
        return True
    # One form may cite additional conditions of the same measurement.
    location = cited(passage)
    shared_support = (listed & attached) - {location}
    return bool(shared_support) and location in listed and (listed < attached or attached < listed)


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


def reviewed_claim_passage(snapshot, ref, observed):
    passage, = _references(snapshot, [ref], observed, "", required=True)
    pdf = (snapshot.get("conclusion") or {}).get("artifact") or _submission(snapshot)[1]
    if passage["artifact"] != pdf or not passage.get("quote"):
        raise ValueError("Locate the author's actual claim in observed text of the submitted PDF.")
    return passage


def _procedure_passage(snapshot, passage):
    return passage["page"] == (snapshot.get("conclusion") or {}).get("page") and any(
        parts["continuation"].split(maxsplit=1)[0].casefold() == "through"
        and _contains_observed_quote(passage["quote"], parts["continuation"])
        for parts in snapshot["evidence_scope"].get("conclusion_clauses", ()))


def _needs_inference_test(snapshot):
    design = snapshot.get("registered_design")
    return bool(snapshot.get("method_control") and design and
                (design.get("observation") or {}).get("status") != "recorded" and
                snapshot.get("evidence_scope", {}).get("conclusion_clauses"))


def _cited_measurement_columns(snapshot, evidence):
    from ark.measurement_table import describe

    columns = set()
    for ref in evidence:
        if snapshot["artifacts"][ref["artifact"]]["kind"] != "measurement":
            continue
        if ref.get("column"):
            columns.add(ref["column"])
            if ref.get("statistic") == "paired":
                columns.add(ref["reference"])
            continue
        quote = ref.get("quote", "").strip()
        try:
            # A reviewer can cite a whole row, a single JSON field, or an entire table.
            data = json.loads(quote if quote.startswith(("{", "[")) else "{" + quote + "}")
        except (ValueError, RecursionError):
            continue
        tables = describe(data)
        if isinstance(data, dict):
            tables.extend(describe([data]))
        columns.update(column for table in tables for column in table["columns"])
    return columns


def _comparison_matches(passage, target, selector):
    if not target or not _quotes_conclusion([passage], target):
        return False
    # A conclusion can contain several comparisons. Each pair binds to its own sentence.
    return selector not in (*SUPERIORITY_SELECTORS, *NUMERIC_SELECTORS) or _normalized_quote(passage["quote"]) == _normalized_quote(target["quote"])


def _comparison_bindings(snapshot, value, passage, evidence, columns):
    """Bind both quoted subjects to measurements that the reviewer actually saw."""
    from ark.measurement_table import project_rows

    sides = value.get("comparison")
    if not isinstance(sides, list) or len(sides) != 2:
        raise ValueError("Give comparison with exactly two subjects from the passage and their measured columns.")
    rows = []
    for ref in evidence:
        if snapshot["artifacts"][ref["artifact"]]["kind"] != "measurement":
            continue
        if ref.get("statistic") == "paired":
            pair = json.loads(ref["quote"])
            fields = {**pair["condition"], ref["column"]: pair["value"], ref["reference"]: pair["reference"]}
            projected = {}
            for path, number_or_label in fields.items():
                branch = projected
                *parents, key = path.split(".")
                for parent in parents:
                    branch = branch.setdefault(parent, {})
                branch[key] = number_or_label
            rows.append(projected)
            continue
        if ref.get("statistic"):
            continue
        quote = ref.get("quote", "").strip()
        try:
            data = json.loads(quote if quote.startswith(("{", "[")) else "{" + quote + "}")
        except (ValueError, RecursionError):
            continue
        if isinstance(data, dict):
            rows.append(data)
        for table in project_rows(data).values():
            rows.extend(table)

    def field(row, column):
        for key in column.split("."):
            if not isinstance(row, dict):
                return None
            row = row.get(key)
        return row

    verified = []
    for item in sides:
        if not isinstance(item, dict):
            raise ValueError("Each comparison subject needs its own measured column.")
        subject = _string(item.get("subject"), "quoted comparison subject", limit=500)
        column = item.get("column")
        if not _contains_observed_quote(passage["quote"], subject) or column not in columns:
            raise ValueError("Quote both subjects from the located passage and bind each to a cited measured column.")
        side = {"subject":subject, "column":column}
        if "condition" in item:
            cond = item["condition"]
            if (not isinstance(cond, dict) or cond.keys() != {"column", "value"}
                    or not isinstance(cond["column"], str) or not cond["column"] or cond["column"] == column
                    or type(cond["value"]) not in (str, int, float)
                    or not any(type(field(row, column)) in (int, float)
                               and type(field(row, cond["column"])) is type(cond["value"])
                               and field(row, cond["column"]) == cond["value"] for row in rows)):
                raise ValueError("Cite an observed row containing both the numeric outcome and its exact condition.")
            side["condition"] = cond
        elif not any(type(field(row, column)) in (int, float) for row in rows) and not any(
                r.get("column") == column and r.get("statistic") != "count" for r in evidence):
            raise ValueError("Each comparison column needs an observed numeric outcome or statistic.")
        verified.append(side)
    left, right = verified
    if left["column"] == right["column"] and all("condition" not in side and "operand" not in item
                                                  for side, item in zip(verified, sides)):
        # A literal label such as "policy B" can identify its row without making the
        # reviewer repeat {"column":"policy","value":"B"} for each quoted subject.
        def label_conditions(side):
            matches = set()
            for row in rows:
                if type(field(row, side["column"])) not in (int, float):
                    continue
                for key, val in row.items():
                    if isinstance(val, str) and _normalized_quote(side["subject"]).casefold() in (
                            val.casefold(), f"{key} {val}".casefold()):
                        matches.add((key, val))
            return dict(matches) if len(matches) == 1 else {}
        left_labels, right_labels = label_conditions(left), label_conditions(right)
        if left_labels and left_labels.keys() == right_labels.keys() and left_labels != right_labels:
            for side, labels in ((left, left_labels), (right, right_labels)):
                key, val = next(iter(labels.items()))
                side["condition"] = {"column":key, "value":val}
    if _normalized_quote(left["subject"]).casefold() == _normalized_quote(right["subject"]).casefold():
        raise ValueError("Identify the two distinct subjects compared in the passage.")
    if left["column"] == right["column"]:
        if "condition" in left or "condition" in right:
            if (not all("condition" in side for side in verified)
                    or left["condition"]["column"] != right["condition"]["column"]
                    or left["condition"]["value"] == right["condition"]["value"]):
                raise ValueError("Bind the common outcome to two distinct values of the same condition column.")
        else:
            if not value.get("contrast_expression"):
                raise ValueError("Both sides use the same outcome. Cite a complete row for each side and give each "
                                 'condition={"column":"group identifier","value":"actual value"} inside comparison. '
                                 "Only a single precomputed difference needs the executed paired contrast expression.")
            expression = _string(value["contrast_expression"], "executed paired contrast expression", limit=1000)
            method = (snapshot.get("measurement_method") or {}).get("artifact")
            operands = [side.get("operand") for side in sides]
            if (not all(isinstance(op, str) and re.fullmatch(r"[A-Za-z_]\w*", op)
                        and re.search(r"\b" + re.escape(op) + r"\b", expression) for op in operands)
                    or operands[0] == operands[1]
                    or not any(r["artifact"] == method and _contains_observed_quote(r.get("quote", ""), expression)
                               for r in evidence)):
                raise ValueError("Cite the executed paired contrast and identify its two distinct operands.")
            for side, operand in zip(verified, operands):
                side["operand"] = operand
            return {"comparison":verified, "contrast_expression":expression}
    return {"comparison":verified}


def _check_condition_assertion(snapshot, selector, assertion, claim, evidence):
    """Recompute the stated numerical relation from the same sealed table as the cited comparison."""
    from ark.measurement_table import summarize
    expected = "signed_difference" if selector == "ordered_change" else "fraction_reduction_in_magnitude"
    recipes = {(r["artifact"],r.get("rows_pointer",""),r["column"],r["reference"],tuple(r["condition_columns"]))
               for r in evidence if r.get("statistic") == "paired"}
    recorded = claim.get("recorded_column")
    pointer = claim.get("rows_pointer", "")
    if recorded and not recipes:
        from ark.measurement_table import project_rows, _value
        sources = [r for r in evidence if snapshot["artifacts"][r["artifact"]]["kind"] == "measurement"]
        measured = {r["artifact"] for r in sources}
        unit = claim.get("unit")
        axis = assertion["axis"]
        if (len(measured) != 1 or not isinstance(recorded,str) or not isinstance(pointer,str)
                or recorded == axis or recorded not in _cited_measurement_columns(snapshot, sources)
                or (unit not in ("fraction", "percent") if selector == "fractional_change" else unit != "value")):
            raise ValueError("Cite the actual recorded change column and give its unit (fraction, percent, or value).")
        artifact, = measured
        data, _ = _artifact(snapshot, artifact)
        rows = project_rows(json.loads(data)).get(pointer, [])
        by_level = {}
        for row in rows:
            key, value = _value(row,axis), _value(row,recorded)
            if (type(key) not in (int,float) or type(value) not in (int,float)
                    or not math.isfinite(key) or not math.isfinite(value) or key in by_level):
                raise ValueError("The recorded changes require one finite value per numeric condition.")
            by_level[key] = value / 100 if unit == "percent" else value
        detail = {"column":recorded, "unit":unit}
    else:
        if claim.get("quantity") != expected or len(recipes) != 1:
            raise ValueError(f"Choose the paper's two outcome fields and cite a calculated paired row with quantity={expected}. "
                             "If the worker directly recorded this change, cite its numeric field as recorded_column and unit. "
                             "For an unsupported claim cite the available evidence and open a finding.")
        artifact, pointer, column, reference, keys = recipes.pop()
        if len(keys) != 1 or keys[0].casefold() != assertion["axis"].casefold():
            raise ValueError("Choose the condition column named by the claim. For an unresolved mapping, report the gap.")
        axis = keys[0]
        data, _ = _artifact(snapshot, artifact)
        rows = summarize(json.loads(data), pointer, column, reference, list(keys))["per_condition"]
        by_level = {r["condition"][axis]:r[expected] for r in rows}
        detail = {"column":column,"reference":reference}
    if selector == "fractional_change":
        levels = assertion["levels"]
        bound, rel = assertion["threshold"], assertion["relation"]
        for level in levels:
            value = by_level.get(level)
            if value is None:
                raise ValueError(f"The fractional reduction has no defined value at {axis}={level:g}.")
            supported = {"more than":value > bound, "over":value > bound, "less than":value < bound,
                         "under":value < bound, "at least":value >= bound, "at most":value <= bound}
            if not supported[rel]:
                raise ValueError(f"At {axis}={level:g} the observed {expected} is {value:.10g}, contradicting "
                                 f"{rel} {bound:g}. Verify the fields or record an unsupported finding.")
    else:
        if len(by_level) < 2 or any(type(k) not in (int,float) for k in by_level):
            raise ValueError("A numerical trend requires at least two ordered conditions.")
        levels = sorted(by_level)
        values = [by_level[k] for k in levels]
        changes = [b-a for a,b in zip(values, values[1:])]
        if (max(changes) <= 0 if assertion["increasing"] else min(changes) >= 0):
            raise ValueError(f"The observed {expected} runs from {values[0]:.10g} to {values[-1]:.10g} and never "
                             "moves in the stated direction. Verify the fields or record an unsupported finding.")
    return {"quantity":expected, "artifact":artifact, "rows_pointer":pointer, **detail,
            "condition":axis, "levels":levels, "values":[by_level[k] for k in levels]}


def _manuscript_claims(snapshot, values, observed, blind, *, require_coverage=True):
    if not isinstance(values, list) or not 1 <= len(values) <= 12:
        raise ValueError("Supply checks.manuscript.claims with the measured result and any separate explanation.")
    claims = []
    for value in values:
        if not isinstance(value, dict) or value.get("kind") not in ("result", "explanation"):
            raise ValueError("Identify each manuscript claim as a result or an explanation.")
        kind, basis = value["kind"], value.get("basis")
        if basis not in (("measurement", "unsupported") if kind == "result" else
                         ("control", "derivation", "qualified", "unsupported", "procedure")):
            raise ValueError("Match the claim basis to its kind. A measured outcome alone cannot establish its explanation.")
        passage = reviewed_claim_passage(snapshot, value.get("passage"), observed)
        evidence = [passage] if basis == "qualified" and "evidence" not in value else _references(
            snapshot, value.get("evidence"), observed, blind, required=True)
        reason = _string(value.get("reason"), "claim-specific reasoning")
        measured = _covers(snapshot, "experiment", evidence)
        claim = dict(kind=kind, passage=passage, basis=basis, evidence=evidence, reason=reason)
        if basis == "measurement" and not measured:
            raise ValueError("Cite the observed outcome for the measured result.")
        if basis == "procedure":
            if (not _procedure_passage(snapshot, passage) or not any(
                    ref.get("quote") and snapshot["artifacts"][ref["artifact"]]["kind"] in ("method", "protocol")
                    for ref in evidence)):
                raise ValueError("Use procedure only for a through-clause describing the observed protocol or implementation.")
        if basis == "control" and not (measured and any(
                snapshot["artifacts"][ref["artifact"]]["kind"] == "method" and ref.get("quote") for ref in evidence)):
            raise ValueError("For a control cite the measured comparison and the observed method that distinguishes the mechanism.")
        if basis in ("control", "measurement") and any(
                _comparison_matches(passage, snapshot["evidence_scope"].get(key), key)
                for key in ("contribution","direction",*SUPERIORITY_SELECTORS)):
            columns = value.get("measured_columns")
            available = _cited_measurement_columns(snapshot, evidence)
            if (not isinstance(columns, list) or not 1 <= len(columns) <= 16
                    or not all(isinstance(c, str) and c in available for c in columns)):
                raise ValueError("For this comparison, name the measured_columns that establish the stated relation "
                                 "and cite those fields in an observed worker row or statistic. If the study "
                                 "measured only a proxy, assess the unresolved claim.")
            claim["measured_columns"] = list(dict.fromkeys(columns))
            if any(_comparison_matches(passage, snapshot["evidence_scope"].get(key), key) for key in SUPERIORITY_SELECTORS):
                claim.update(_comparison_bindings(snapshot, value, passage, evidence, columns))
        checks_observed_values = basis in ("measurement", "control") or (basis == "derivation" and re.search(
            r"\b(?:as shown in the results|we (?:measured|observed)|the measured|our findings)\b",
            _assertion_text(passage["quote"]), re.I))
        if checks_observed_values:
            for key in NUMERIC_SELECTORS:
                target = snapshot["evidence_scope"].get(key)
                if _comparison_matches(passage, target, key):
                    claim["condition_check"] = _check_condition_assertion(snapshot, key, target["assertion"], value, evidence)
        inference_tests = [r for r in observed if r.get("control") == 3
                           and r.get("revision") == snapshot["revision"] and r.get("passage") == passage]
        if basis == "derivation":
            reasoning = [ref for ref in evidence if ref.get("quote")
                         and not _contains_observed_quote(passage["quote"], ref["quote"])
                         and snapshot["artifacts"][ref["artifact"]]["kind"] in ("submission", "primary_source", "method")]
            if not reasoning:
                raise ValueError("Cite a separate, observed derivation connecting the explanation to the measured outcome. "
                                 "A measured row or your own unsupported formula does not establish the authors' cause. "
                                 "If that calculation is absent, use basis=unsupported with a finding and cite the "
                                 "actual measurements, or quote the authors' own qualification if present.")
            if snapshot["evidence_scope"].get("version") == 4:
                bridge = value.get("bridge")
                if isinstance(bridge, dict) and bridge.keys() == {"evidence"}:
                    n = bridge["evidence"]
                    if (type(n) is not int or not 1 <= n <= len(evidence)
                            or "block" not in value["evidence"][n - 1] or evidence[n - 1] not in reasoning
                            or _contains_observed_quote(evidence[n - 1]["quote"], passage["quote"])):
                        raise ValueError("Choose a separate observed PDF reasoning block by its one-based position in evidence.")
                    bridge = evidence[n - 1]["quote"]
                bridge = _string(bridge, "bridge from the derivation to the claimed outcome", limit=1200)
                sources = [ref for ref in reasoning if _contains_observed_quote(ref["quote"], bridge)]
                if not sources:
                    raise ValueError("The bridge must quote the observed reasoning cited in this claim.")
                claim["bridge"] = bridge
                self_cited = all(snapshot["artifacts"][r["artifact"]]["kind"] == "submission" for r in sources)
                if (_needs_inference_test(snapshot) and self_cited
                        and passage["page"] == snapshot["conclusion"]["page"]
                        and any(_contains_observed_quote(passage["quote"], part["continuation"])
                                for part in snapshot["evidence_scope"]["conclusion_clauses"])
                        and not any(r["same_premise"] for r in inference_tests)):
                    raise ValueError("The registered comparison did not measure this mechanism in full. "
                                     "Before relying on the manuscript's derivation for this empirical conclusion, "
                                     "use test_inference on this passage with the premise held constant. "
                                     "Compare the outcome named in the conclusion, or revise the assessment using available evidence.")
                if snapshot.get("method_control") and not inference_tests and self_cited:
                    instruction = ("Check the submission's derivation at this exact passage with test_inference "
                                   "or supply independent_derivation with your own analytic calculation from the "
                                   "assumptions to the claimed outcome. If the implication fails, report the gap.")
                    calculation = value.get("independent_derivation")
                    if not isinstance(calculation, str) or not calculation.strip():
                        raise ValueError(instruction)
                    calculation = _string(calculation, "independent derivation", limit=2400)
                    if any(_contains_observed_quote(ref["quote"], calculation) for ref in sources):
                        raise ValueError(instruction)
                    claim["independent_derivation"] = calculation
        if kind == "explanation" and basis in ("derivation", "control") and any(
                r["same_premise"] and r["distinguishable_outcome"] for r in inference_tests):
            claim["counterexample_limit"] = _string(value.get("counterexample_limit"),
                                                      "reason the study excludes the constructed counterexample")
            if _needs_inference_test(snapshot):
                restriction = _references(snapshot, value.get("exclusion_evidence"), observed, blind, required=True)
                if not any(ref.get("quote") and snapshot["artifacts"][ref["artifact"]]["kind"] in
                           ("protocol", "measurement", "primary_source", "method") for ref in restriction):
                    raise ValueError("Cite the observed assumption or measurement excluding the counterexample in exclusion_evidence.")
                claim["exclusion_evidence"] = restriction
                claim["evidence"] = evidence + [ref for ref in restriction if ref not in evidence]
        if basis == "qualified":
            wording = _interpretation_wording(passage, {"claim":value.get("assertion", passage["quote"]),
                                                       "qualification":value.get("qualification")})
            claim["qualification"] = wording["qualification"]
        if "finding" in value:
            finding = value["finding"]
            if basis != "unsupported" or not isinstance(finding, dict) or finding.keys() != {"id", "fix"}:
                raise ValueError("An inline finding requires an unsupported claim, a finding ID and the proposed fix.")
            claim["finding"] = {"id":_string(finding["id"], "finding ID", limit=5),
                                "fix":_string(finding["fix"], "proposed fix")}
        claims.append(claim)
    if not require_coverage:
        return claims
    if not any(c["kind"] == "result" for c in claims):
        raise ValueError("Identify the empirical result separately from its explanation.")
    target = snapshot.get("conclusion")
    for parts in snapshot["evidence_scope"].get("conclusion_clauses", ()):
        def covers(claim, wording):
            return (claim["passage"]["page"] == target["page"]
                    and _contains_observed_quote(claim["passage"]["quote"], wording))
        if not any(c["kind"] == "explanation" and covers(c, parts["continuation"]) for c in claims):
            raise ValueError(f'Assess the continuation on page {target["page"]} separately. Use kind=explanation '
                             f'and passage={{"selector":"conclusion","sentence":{parts["sentence"]}}}. '
                             'Rows supporting the outcome cannot by themselves support the rest of the sentence.')
        for claim in claims:
            if claim["kind"] == "result" and covers(claim, parts["continuation"]):
                if not covers(claim, parts["result"]):
                    raise ValueError("Cite the observed outcome clause for the result.")
                claim["passage"] = {**claim["passage"], "quote":parts["result"]}
    locator = snapshot["evidence_scope"].get("attribution")
    if locator and not any(c["kind"] == "explanation" and _quotes_conclusion([c["passage"]], locator) for c in claims):
        raise ValueError('Assess the located attribution separately as an explanation with passage={"selector":"attribution"}.')
    missing = []
    for selector in COMPARISON_SELECTORS:
        target = snapshot["evidence_scope"].get(selector)
        if target and not any(_comparison_matches(c["passage"], target, selector) and c["basis"] != "procedure" for c in claims):
            missing.append(f'{selector} comparison on page {target["page"]} with passage={{"selector":"{selector}"}}')
    if missing:
        raise ValueError("Assess each located comparison separately: " + ", ".join(missing) + ".")
    return claims


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
    return _contains_observed_quote(text, ref["quote"])


def _tex_paragraph_locator(source):
    """Locate a whole rendered sentence uniquely in ordinary TeX prose, allowing inline math."""
    def letters(text):
        return re.sub(r"[\W_]", "", unicodedata.normalize("NFKC", text).casefold())

    # A hard paragraph boundary must not disappear when headings or environments
    # print no prose. Leave unsupported TeX constructs out of the matching index.
    source = re.sub(r"(?<!\\)%[^\n]*", "", source)
    boundaries = r"(\n\s*\n|\\par\b|(?m:^[ \t]*\\(?:begin|end|(?:sub)*section|chapter|paragraph)\*?\{[^\n]*))"
    # Keep structural markers as context for aligning a one-paragraph revision.
    paragraphs = [" ".join(part.split()) for part in re.split(boundaries, source)
                  if part.strip() and part != r"\par"]
    patterns = []
    inline_math = re.compile(r"\$[^$\n]+\$|\\\([^\n]*?\\\)")
    for number, paragraph in enumerate(paragraphs):
        plain = inline_math.sub("", paragraph)
        if re.search(r"\\(?!(?:texttt|textbf|textit|emph|ref|cite[tp]?)\b|[%&_])", plain):
            continue
        for sentence in _conclusion_sentences(paragraph):
            parts = inline_math.split(sentence)
            if any("\\" in part for part in parts):
                continue
            pieces = [letters(part) for part in parts]
            if sum(map(len, pieces)) >= 24:
                patterns.append((number, sentence, pieces))

    def locate(quote, *, with_source=False):
        key = letters(quote)
        matches = []
        for number, sentence, pieces in patterns:
            if len(pieces) == 1:
                if key == pieces[0]:
                    matches.append((number, sentence))
                continue
            if not key.startswith(pieces[0]) or not key.endswith(pieces[-1]):
                continue
            pos = len(pieces[0])
            for mid in pieces[1:-1]:
                found = key.find(mid, pos, len(key) - len(pieces[-1]))
                if found < 0:
                    break
                pos = found + len(mid)
            else:
                if pos <= len(key) - len(pieces[-1]):
                    matches.append((number, sentence))
        if len(matches) == 1:
            return matches[0] if with_source else matches[0][0]
    return locate, paragraphs


def _add_paragraph_continuation(context, stable, old_par, old_locate, new_locate, old_pdf, old_page, blocks, wording):
    # Page adjacency alone could associate a float or a new paragraph.
    # Require a common author paragraph in both TeX sources and a complete
    # surviving PDF sentence separate from the anchor in both rendered revisions.
    if old_par is None:
        return
    anchors = {new_locate(q) for q in stable if old_locate(q) == old_par}
    anchors.discard(None)
    if len(anchors) != 1:
        return
    new_par = anchors.pop()
    from collections import Counter
    old_nearby = Counter(wording(q) for idx in range(max(0, old_page - 2), min(len(old_pdf), old_page + 1))
                         for block in old_pdf[idx].get_text("blocks") if block[6] == 0
                         for q in _conclusion_sentences(block[4]))
    adjacent = [b for b in blocks if abs(b[0] - context["page"]) == 1]
    occurrences = Counter(wording(q) for _, _, block, _ in adjacent for q in _conclusion_sentences(block))
    continuation = []
    for page, offset, block, candidates in adjacent:
        for key, quote in candidates.items():
            if (old_nearby[key] == occurrences[key] == 1 and 20 <= len(quote) <= 1200
                    and old_locate(quote) == old_par and new_locate(quote) == new_par):
                at = offset + block.index(quote)
                continuation.append((quote, {"page":page, "start":at, "end":at + len(quote) - 1}))
    if continuation:
        locations = []
        for quote in context["remaining"]:
            at = context["start"] + context["paragraph"].index(quote)
            locations.append({"page":context["page"], "start":at, "end":at + len(quote) - 1})
        context["remaining"].extend(quote for quote, _ in continuation)
        context["remaining_locations"] = locations + [location for _, location in continuation]


def _reflowed_paragraph(pdf, blocks, signed_claim, old_locate, new_locate, aligned):
    """Recover a continuation after the author replaced every anchor on the original page."""
    old_par = old_locate(signed_claim)
    if old_par not in aligned:
        return None
    passages = []
    for page, start, block, _ in blocks:
        for quote in _conclusion_sentences(block):
            if not 20 <= len(quote) <= 1200 or not quote.endswith((".", "!", "?")):
                continue
            prior = old_locate(quote, with_source=True)
            if not prior or prior[0] != old_par:
                continue
            current = new_locate(quote, with_source=True)
            # Match full TeX sentences including their math. Rendered math may
            # omit TeX delimiters, but a changed operand cannot count as survival.
            if current != (aligned[old_par], prior[1]):
                continue
            at = start + block.index(quote)
            passages.append((quote, {"page":page, "start":at, "end":at + len(quote) - 1}, block, start))
    # Identical running text printed twice is ambiguous even after rewrapping.
    identities = {re.sub(r"[\W_]", "", unicodedata.normalize("NFKC", q).casefold()) for q, *_ in passages}
    if not passages or len(identities) != len(passages):
        return None
    _, first, block, start = passages[0]
    remaining = [q for q, *_ in passages]
    return {"artifact":pdf, "page":first["page"], "start":start, "paragraph":block,
            "remaining":remaining, "remaining_locations":[location for _, location, _, _ in passages],
            "surviving":remaining}


def revision_paragraphs(snapshot, previous):
    """Locate surviving neighbors of disputed sentences in the current rendered body."""
    from difflib import SequenceMatcher
    import fitz
    from ark.latex.utils import body_page_boundary

    if (not previous or not snapshot.get("evidence_scope", {}).get("recheck_paragraphs")
            or not re.fullmatch(r"[0-9a-f]{64}", str(previous.get("revision", "")))
            or previous["revision"] == snapshot["revision"]):
        return {}
    tex, pdf = _submission(snapshot)
    blockers = [i for i in previous.get("issues", []) if i["domain"] == "manuscript"
                and i["severity"] == "blocker" and i["status"] != "resolved"]
    if not blockers or not pdf:
        return {}
    archived = _inside(Path(snapshot["project"]), Path(STATE) / "reviews" / previous["revision"] / "main.pdf")
    if not archived.is_file() or archived.stat().st_size > MAX_BYTES:
        return {}
    original = archived.read_bytes()
    if _digest(original) != previous.get("artifact_hashes", {}).get(pdf):
        raise ValueError("The archived PDF does not match the preceding review.")
    # Match on complete sentences. TeX may hyphenate and paginate an unchanged
    # sentence differently on a later build. Never credit a fuzzy quotation.
    def wording(sentence):
        return _normalized_quote(re.sub(r"(?<=\w)-[ \t]*\n(?=[a-z])", "", sentence))

    old_locate = new_locate = None
    aligned_paragraphs = {}
    source_hash = previous.get("artifact_hashes", {}).get(tex)
    if source_hash and tex in snapshot["artifacts"]:
        archived_tex = _inside(Path(snapshot["project"]), Path(STATE) / "reviews" / previous["revision"] / "main.tex")
        if archived_tex.is_file() and archived_tex.stat().st_size <= MAX_BYTES:
            source = archived_tex.read_bytes()
            if _digest(source) != source_hash:
                raise ValueError("The archived TeX does not match the preceding review.")
            old_locate, old_paragraphs = _tex_paragraph_locator(source.decode("utf-8", "replace"))
            new_locate, new_paragraphs = _tex_paragraph_locator(_artifact(snapshot, tex)[0].decode("utf-8", "replace"))
            # Splitting a paragraph or moving a sentence between independent
            # paragraphs is ambiguous. Only a one-for-one substitution between
            # unchanged source boundaries can recover a lost rendered anchor.
            alignment = SequenceMatcher(None, old_paragraphs, new_paragraphs, autojunk=False)
            aligned_paragraphs = {i:a for tag, i, j, a, b in alignment.get_opcodes()
                                  if tag == "replace" and j - i == b - a == 1}

    result = {}
    with fitz.open(stream=original, filetype="pdf") as old, fitz.open(stream=_artifact(snapshot, pdf)[0], filetype="pdf") as new:
        boundary = body_page_boundary(new)
        end = boundary[0] + int(any(boundary[1])) if boundary else len(new)
        blocks = []
        for index in range(min(len(new), end)):
            page = new[index]
            text = page.get_text()
            for block in page.get_text("blocks"):
                paragraph = block[4].strip()
                if block[6] != 0 or not 30 <= len(paragraph) <= VIEW_SIZE:
                    continue
                offset = text.find(paragraph)
                if offset >= 0:
                    sentences = _conclusion_sentences(paragraph)
                    blocks.append((index + 1, offset + 1, paragraph, {wording(q):q for q in sentences}))
        for issue in blockers:
            claim = wording(issue["claim"])
            for ref in issue["evidence"]:
                page = ref.get("page", 0)
                if (ref["artifact"] != pdf or not 1 <= page <= len(old)
                        or claim not in wording(ref.get("quote", ""))):
                    continue
                paragraphs = [block[4] for block in old[page - 1].get_text("blocks")
                              if block[6] == 0 and claim in wording(block[4])]
                if len(paragraphs) != 1:
                    continue
                sentences = [wording(q) for q in _conclusion_sentences(paragraphs[0])]
                disputed = next((i for i, q in enumerate(sentences) if claim in q or q in claim), None)
                if disputed is None:
                    continue
                neighbors = {q for q in sentences if q not in claim and 20 <= len(q) <= 1000}
                matches = [(len(neighbors.intersection(keys)), n) for n, (_, _, _, keys) in enumerate(blocks)]
                count = max((score for score, _ in matches), default=0)
                best = [n for score, n in matches if score == count]
                if not count or len(best) != 1:
                    if aligned_paragraphs:
                        reflowed = _reflowed_paragraph(pdf, blocks, sentences[disputed], old_locate,
                                                       new_locate, aligned_paragraphs)
                        if reflowed:
                            result[issue["id"]] = reflowed
                            break
                    continue
                new_page, start, paragraph, keys = blocks[best[0]]
                terms = set(re.findall(r"[a-z]{4,}", (claim + " " + issue["reason"]).casefold()))
                surviving = [i for i, q in enumerate(sentences) if q in neighbors and q in keys]
                # Include the next surviving assertion and the statement most
                # closely related to the old finding, even if it preceded the edit.
                following = next((i for i in surviving if i > disputed), None)
                ranked = sorted(surviving, key=lambda i: (
                    -len(terms.intersection(re.findall(r"[a-z]{4,}", sentences[i].casefold()))), abs(i - disputed)))
                selected = list(dict.fromkeys([*([following] if following is not None else []), *ranked]))[:2]
                result[issue["id"]] = {"artifact":pdf, "page":new_page, "start":start,
                                       "paragraph":paragraph, "remaining":[keys[sentences[i]] for i in selected]}
                if not any(claim in wording(block[2]) for block in blocks):
                    # A paragraph can acquire several unrelated sentences. Locate
                    # the edit aligned to the original assertion between stable neighbors.
                    current = _conclusion_sentences(paragraph)
                    alignment = SequenceMatcher(None, sentences, [wording(q) for q in current], autojunk=False)
                    result[issue["id"]]["replacement"] = [
                        q for op, i, j, a, b in alignment.get_opcodes() if op == "replace" and i <= disputed < j
                        for q in current[a:b] if len(q) <= 1200]
                    result[issue["id"]]["surviving"] = [keys[sentences[i]] for i in surviving]
                if old_locate and new_locate:
                    old_par = old_locate(sentences[disputed])
                    stable = [keys[sentences[i]] for i in surviving]
                    _add_paragraph_continuation(result[issue["id"]], stable, old_par, old_locate, new_locate,
                                                old, page, blocks, wording)
                break
    return result


def _context_support(snapshot, item, ref, context, claims, observed, blind):
    """Bind each closure judgment to the author's words and that sentence's own evidence."""
    basis, quote = item.get("judgment"), ref["quote"]
    if basis == "limited":
        return _interpretation_wording({"quote":context["paragraph"]},
                                      {"claim":quote, "qualification":item.get("qualification")})
    if basis == "derived":
        n = item.get("claim_ref")
        if type(n) is int and 1 <= n <= len(claims):
            claim = claims[n - 1]
            if (claim["basis"] == "derivation" and claim["passage"] == ref and claim.get("bridge")
                    and (claim.get("independent_derivation")
                         or any(snapshot["artifacts"][e["artifact"]]["kind"] == "primary_source"
                                and _contains_observed_quote(e.get("quote", ""), claim["bridge"]) for e in claim["evidence"])
                         or any(r.get("control") == 3 and r.get("revision") == snapshot["revision"]
                                and r.get("passage") == ref and r.get("same_premise") for r in observed))):
                return {"claim_ref":n, "evidence":claim["evidence"]}
        raise ValueError("For derived, reference an assessed derivation of this exact remaining sentence with "
                         "an independent calculation, observed primary-source bridge or independent inference test.")
    if basis not in ("measured", "documented"):
        raise ValueError("Each remaining sentence needs its own measured, documented, derived or author-limited "
                         "basis. A distinct topic or conceptual reading alone cannot close a blocker.")
    refs = _references(snapshot, item.get("evidence"), observed, blind, required=True)
    if basis == "measured":
        quantity = _string(item.get("quantity"), "printed measured quantity", limit=160)
        columns = item.get("measured_columns")
        available = _cited_measurement_columns(snapshot, refs)
        # Compare the printed metric name to whole words of a cited field,
        # so e.g. 'mass loss' can identify 'mean_mass_loss_treated'.
        def words(text):
            return re.findall(r"[^\W_]+", text.casefold())
        if (not _contains_observed_quote(quote, quantity) and isinstance(columns, list)
                and quantity in columns and all(isinstance(c, str) and c in available for c in columns)):
            # A reviewer may give its observed column as the label. Only map a unique
            # printed acronym present as a whole word in every cited column.
            acronyms = {label for label in re.findall(r"\b[A-Z][A-Z0-9]{1,9}\b", quote)
                        if all(label.casefold() in words(column) for column in columns)}
            if len(acronyms) == 1:
                quantity = acronyms.pop()
        metric = words(quantity)
        def names_metric(column):
            name = words(column)
            if any(name[i:i+len(metric)] == metric for i in range(len(name))):
                return True
            # Some protocols call the endpoint by its units or a descriptive alias.
            # Accept an observed definition binding that name to the worker field.
            return any(snapshot["artifacts"][e["artifact"]]["kind"] in ("method","protocol","primary_source")
                       and any(len(line) <= 240 and _contains_observed_quote(line, quantity) and column in line
                               for line in e.get("quote", "").splitlines()) for e in refs)
        if (not metric or not _contains_observed_quote(quote, quantity) or not isinstance(columns, list)
                or not 1 <= len(columns) <= 16 or not all(isinstance(c, str) and c in available
                                                          and names_metric(c) for c in columns)):
            raise ValueError("Name the printed measured quantity and its columns in this sentence's observed "
                             "worker evidence, or cite the protocol or method mapping the printed alias to the field. "
                             "If the measurement does not address the claim, leave it open or unassessed.")
        return {"evidence":refs, "quantity":quantity, "measured_columns":list(dict.fromkeys(columns))}
    for evidence in refs:
        kind = snapshot["artifacts"][evidence["artifact"]]["kind"]
        cited = evidence.get("quote", "")
        if kind == "primary_source" and _contains_observed_quote(cited, quote):
            return {"evidence":refs}
        if kind != "protocol":
            continue
        import yaml
        protocol = yaml.safe_load(_artifact(snapshot, evidence["artifact"])[0])
        from ark.compute.native_experiment import plan_digest
        policy = snapshot.get("evidence_scope", {}).get("recheck_paragraphs")
        if (not isinstance(protocol, dict) or not isinstance(policy, dict)
                or plan_digest(protocol) != policy.get("plan_sha256")):
            continue
        controls = protocol.get("controls")
        operational = [protocol.get("baseline"), protocol.get("comparison"), *(controls if isinstance(controls, list) else [])]
        for step in operational:
            if not isinstance(step, str) or not _contains_observed_quote(cited, step):
                continue
            if _contains_observed_quote(step, quote):
                return {"evidence":refs}
            # Canonicalize an ordinary active/passive restatement without discarding
            # operand order, numbers, units, negation, or mathematical symbols.
            def operation_text(text):
                text = _normalized_quote(text).casefold().strip(". ")
                passive = re.fullmatch(r"(.+?) (?:was|were|is|are) (\w+) (?:by|for) (.+)", text)
                if passive:
                    text = f"{passive[3]} {passive[2]} {passive[1]}"
                return re.sub(r"\b(?:a|an|the) ", "", text)
            executed = (snapshot.get("measurement_method") or {}).get("artifact")
            if (item.get("operation") == step and operation_text(quote) == operation_text(step)
                    and any(r["artifact"] == executed and r.get("quote") for r in refs)):
                return {"evidence":refs, "operation":step}
    raise ValueError("For documented, cite the remaining statement directly in a primary source or documented "
                     "experimental procedure. A planned hypothesis or unrelated method cannot establish a result.")


def _resolution_changed(snapshot, previous, old, refs):
    if previous["revision"] == snapshot["revision"]:
        return False
    submission = _submission(snapshot)
    passages = [r for r in old["evidence"] if r["artifact"] in submission]
    # When a finding located the same assertion in multiple sections, editing
    # one appearance cannot discharge the reader's objection to the others.
    # Contextual references (such as a valid method description) can stay.
    disputed = [r for r in passages if r.get("quote") and old.get("claim")
                and _contains_observed_quote(r["quote"], old["claim"])]
    if disputed:
        wording_changed = all(not _passage_survives(snapshot, r) for r in disputed)
    else:
        wording_changed = any(not _passage_survives(snapshot, r) for r in passages)
    if wording_changed:
        return True
    # A previously missing control or source can support an unchanged claim.
    # Require a changed *evidence artifact* and a newly located observation;
    # changing an unrelated sentence in the TeX file cannot clear a blocker.
    return _new_finding_evidence(previous, old, refs, submission)


def _new_finding_evidence(previous, old, refs, submission):
    def observation(r):
        quote = r.get("quote")
        if r["artifact"].lower().endswith(".json"):
            quote = _json_container_tokens(quote) or quote
        return r["artifact"], r.get("page"), quote

    prior_observations = {observation(r) for r in old["evidence"]}
    return any(r["artifact"] not in submission and
               r["sha256"] != previous["artifact_hashes"].get(r["artifact"]) and
               observation(r) not in prior_observations for r in refs)


def _retained_comparison(snapshot, previous, old, issue, refs, context):
    """A revised passage can still be disputed. Tie that judgment to what it now says."""
    claim = _normalized_quote(_string(issue.get("claim"), "current disputed assertion", limit=1200))
    at_paragraph = any(r["artifact"] == context["artifact"] and r.get("page") == context["page"]
                       and _contains_observed_quote(r.get("quote", ""), claim) for r in refs)
    for quote, location in zip(context["remaining"], context.get("remaining_locations", ())):
        if claim == _normalized_quote(quote) and any(
                r["artifact"] == context["artifact"] and r.get("page") == location["page"]
                and _contains_observed_quote(r.get("quote", ""), claim) for r in refs):
            return {}
    if "replacement" not in context:
        raise ValueError(f"{issue['id']} must cite a surviving current sentence from the revised paragraph "
                         "and its observed PDF page, or report the remaining evidence gap as unassessed.")
    if at_paragraph and any(claim == _normalized_quote(q) for q in context["surviving"]):
        return {}
    if (at_paragraph and any(claim == _normalized_quote(q) for q in _conclusion_sentences(context["paragraph"]))
            and _new_finding_evidence(previous, old, refs, _submission(snapshot))):
        return {}
    comparison = issue.get("revision_comparison")
    if at_paragraph and isinstance(comparison, dict) and any(claim == _normalized_quote(q) for q in context["replacement"]):
        earlier, later, reason = (comparison.get(k) for k in ("previous", "current", "reason"))
        if (all(isinstance(v, str) and 2 <= len(v.strip()) <= limit
                for v, limit in ((earlier, 1200), (later, 1200), (reason, 4000)))
                and _contains_observed_quote(old["claim"], earlier) and _contains_observed_quote(claim, later)
                and _normalized_quote(earlier) != _normalized_quote(later)
                and (not _contains_observed_quote(claim, earlier) or not _contains_observed_quote(old["claim"], later))
                and _contains_observed_quote(reason, later) and _normalized_quote(reason) != _normalized_quote(later)
                and any(snapshot["artifacts"][r["artifact"]]["kind"] in
                        ("measurement", "protocol", "method", "primary_source") and r.get("quote") for r in refs)):
            return {"revision_comparison":{"previous":earlier, "current":later, "reason":reason}}
    remaining = context["remaining"]
    if "remaining_locations" in context:
        remaining = [{"page":loc["page"], "quote":q} for q, loc in zip(remaining, context["remaining_locations"])]
    remaining = json.dumps(remaining, ensure_ascii=False)
    raise ValueError(f"{issue['id']} on page {context['page']}: the previous disputed sentence changed. Recheck "
                     f"these surviving sentences: {remaining}. For still_present, quote the complete current "
                     "assertion in claim and cite it on its PDF page. If the replacement itself remains unsupported, "
                     "use an explicit issue with revision_comparison: previous and current quote the changed words "
                     "of each version; reason explains why the current words still overclaim, quoting them. "
                     "Cite a measured row, protocol, method or original source needed to judge that inference. "
                     "Fresh contradictory evidence can also support a located current defect. Otherwise assess "
                     "the remaining context for resolution, or return unassessed.")


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
        claims = None
        if domain == "manuscript" and assessed and snapshot.get("evidence_scope", {}).get("version") in (3, 4):
            claims = _manuscript_claims(snapshot, check.get("claims"), observed, blind)
            target = snapshot.get("conclusion")
            if target and not any(c["passage"]["page"] == target["page"] and _contains_observed_quote(
                    target["quote"], c["passage"]["quote"]) for c in claims):
                raise ValueError('Locate at least one assessed claim in the observed conclusion with the sentence selector.')
            refs = []
            for claim in claims:
                for ref in (claim["passage"], *claim["evidence"]):
                    if ref not in refs:
                        refs.append(ref)
        else:
            refs = _references(snapshot, check.get("evidence", []), observed, blind, required=assessed)
        origins = None
        if assessed and domain == "experiment" and (worker := snapshot.get("worker_measurement")):
            if not worker["verified"]:
                raise ValueError("The current plan has no verified worker output. Mark experiment unassessed.")
            if not any(r["artifact"] == worker["expected"] for r in refs):
                raise ValueError(f"Cite measured rows from the current worker output {worker['expected']}.")
        if assessed:
            _check_source_and_method(snapshot, domain, refs)
        if assessed and not _covers(snapshot, domain, refs):
            raise ValueError(f"No observed {domain} evidence supports this assessment")
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
        if claims is not None:
            checks[domain]["claims"] = claims
        elif domain == "manuscript" and assessed and (target := snapshot.get("conclusion")):
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
            if snapshot.get("evidence_scope", {}).get("version") == 2:
                claims = _manuscript_claims(snapshot, check.get("claims"), observed, blind)
                checks[domain]["claims"] = claims
                if support == "measured" and any(c["basis"] in ("qualified","unsupported")
                        and _contains_observed_quote(target["quote"], c["passage"]["quote"]) for c in claims):
                    raise ValueError("The overall conclusion assessment must reflect the qualified or unsupported claim within it.")
        if domain == "experiment" and (controls or reproductions):
            checks[domain]["controls"] = controls + reproductions
        if domain == "manuscript":
            cases = [receipt for receipt in observed if receipt.get("control") == 3
                     and receipt.get("revision") == snapshot["revision"]]
            if cases:
                checks[domain]["inference_tests"] = list({_canonical(r): r for r in cases}.values())
        if origins:
            checks[domain]["series_origin"] = origins

    prior = {i["id"]: i for i in previous["issues"]} if previous else {}
    prior_open = {key for key, issue in prior.items() if issue["status"] != "resolved"}
    closed_ids = set(previous.get("closed_ids", [])) if previous else set()
    raw_issues = response.get("issues")
    if not isinstance(raw_issues, list):
        raise ValueError("Expected an array of at most 32 review findings")
    raw_issues = list(raw_issues)
    explicit_count = len(raw_issues)
    for index, claim in enumerate(checks["manuscript"].get("claims", ()), 1):
        if finding := claim.get("finding"):
            raw_issues.append({**finding, "claim_ref":index, "reason":claim["reason"], "domain":"manuscript",
                               "severity":"blocker", "status":"still_present" if finding["id"] in prior_open else "new"})
    if len(raw_issues) > 32:
        raise ValueError("Expected an array of at most 32 review findings")
    issues = []
    contexts = revision_paragraphs(snapshot, previous) if any(
        isinstance(i, dict) and i.get("status") in ("resolved", "still_present")
        and i.get("domain") == "manuscript" for i in raw_issues) else {}
    retention_errors = []
    missing_observations = False
    used = set()
    linked = set()
    for position, issue in enumerate(raw_issues):
        if position >= explicit_count and issue["id"] in used and issue["id"] not in linked:
            # Both representations are supplied by the same reviewer. The explicit
            # issue has already passed its own history and evidence checks.
            matching = next(item for item in issues if item["id"] == issue["id"])
            claim = checks["manuscript"]["claims"][issue["claim_ref"] - 1]
            if _same_open_finding(matching, claim, issue["status"]):
                matching["claim"] = claim["passage"]["quote"]
                for ref in claim["evidence"]:
                    if ref not in matching["evidence"]:
                        matching["evidence"].append(ref)
                linked.add(issue["id"])
                continue
        if position >= explicit_count:
            linked.add(issue["id"])
        if isinstance(issue, dict) and "claim_ref" in issue:
            index = issue["claim_ref"]
            claims = checks["manuscript"].get("claims", [])
            if (type(index) is not int or not 1 <= index <= len(claims) or issue.get("domain") != "manuscript"
                    or issue.get("status") not in ("new","still_present") or "claim" in issue or "evidence" in issue):
                raise ValueError("Use claim_ref alone for the words and evidence of an open manuscript finding.")
            claim = claims[index - 1]
            if claim["basis"] != "unsupported":
                raise ValueError("Select an unsupported manuscript claim for the linked finding.")
            issue = {**issue, "claim":claim["passage"]["quote"], "evidence":[claim["passage"], *claim["evidence"]]}
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
        if problem := _issue_history_problem(issue, prior, closed_ids):
            raise ValueError(problem)
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
        assessment = []
        comparison = {}
        context = contexts.get(ident, {})
        if status == "still_present" and "surviving" in context:
            try:
                comparison = _retained_comparison(snapshot, previous, old, issue, refs, context)
            except ValueError as exc:
                retention_errors.append(str(exc))
        if status == "resolved":
            if checks[domain]["status"] != "assessed" or not _covers(snapshot, domain, refs):
                raise ValueError(f"Resolving {ident} requires new verified evidence")
            if not _resolution_changed(snapshot, previous, old, refs):
                raise ValueError(f"No changed observation supports resolving {ident}. Recheck each cited "
                                 "occurrence of the disputed claim, or cite newly observed supporting evidence.")
            if context := contexts.get(ident):
                judgments = issue.get("context_assessment")
                neighbors = context["remaining"]
                indexed = []
                for item in judgments if isinstance(judgments, list) else []:
                    if not isinstance(item, dict):
                        break
                    index = item.get("sentence")
                    if isinstance(index, str):
                        exact = [n for n, quote in enumerate(neighbors, 1)
                                 if _normalized_quote(index) == _normalized_quote(quote)]
                        if len(exact) != 1:
                            break
                        index = exact[0]
                    if type(index) is not int:
                        break
                    indexed.append({**item, "sentence": index})
                if (not isinstance(judgments, list) or len(indexed) != len(judgments)
                        or sorted(j["sentence"] for j in indexed) != list(range(1, len(neighbors) + 1))):
                    pages = sorted({loc["page"] for loc in context.get("remaining_locations", [context])})
                    where = ("page " if len(pages) == 1 else "pages ") + ", ".join(map(str, pages))
                    locations = context.get("remaining_locations", [context] * len(neighbors))
                    passages = [f"{n} (PDF p. {loc['page']}): {' '.join(sentence.split())}"
                                for n, (sentence, loc) in enumerate(zip(neighbors, locations), 1)]
                    raise ValueError(f"Resolving {ident} requires a context_assessment of each remaining sentence "
                                     f"in its revised paragraph on {where}. Use its one-based number or unique full PDF quote. "
                                     "If the unsupported claim survives, keep the blocker still_present and cite that "
                                     "sentence. Recheck these passages against the recorded evidence:\n" + "\n".join(passages))
                for item in indexed:
                    quote = neighbors[item["sentence"] - 1]
                    location = context["remaining_locations"][item["sentence"] - 1] if "remaining_locations" in context else context
                    try:
                        ref, = _references(snapshot, [{"artifact":context["artifact"], "page":location["page"],
                                                       "quote":quote}], observed, blind, required=True)
                        entry = {"sentence":item["sentence"], "judgment":item.get("judgment"), "passage":ref,
                                 "reason":_string(item.get("reason"), "reason for the remaining sentence")}
                        evidence_context = context if location["page"] == context["page"] else {"paragraph":quote}
                        entry.update(_context_support(snapshot, item, ref, evidence_context,
                                                      checks["manuscript"].get("claims", ()), observed, blind))
                    except ValueError as exc:
                        raise ValueError(f"Finding {ident}, PDF page {location['page']}, remaining sentence "
                                         f"{item['sentence']}: {quote}\n{exc}") from None
                    assessment.append(entry)
        issues.append({"id": ident, "status": status, "severity": severity, "domain": domain,
                       **{k: _string(issue.get(k), k) for k in ("claim", "reason", "fix")}, "evidence": refs,
                       **({"context_assessment":assessment} if assessment else {}), **comparison})
    if retention_errors:
        raise ValueError("\n".join(retention_errors))
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
    for claim in checks["manuscript"].get("claims", []):
        passage = claim["passage"]
        if claim["basis"] == "unsupported" and not any(
                issue["status"] in ("new", "still_present") and issue["severity"] == "blocker"
                and _contains_observed_quote(passage["quote"], issue["claim"])
                and any(r["artifact"] == passage["artifact"] and r.get("page") == passage["page"]
                        and _contains_observed_quote(r.get("quote", ""), issue["claim"]) for r in issue["evidence"])
                for issue in issues):
            raise ValueError("Open a blocker on the unsupported claim's PDF page, quote the disputed wording and specify the repair.")
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
        manuscript = (record.get("checks") or {}).get("manuscript") or {}
        if (snapshot["evidence_scope"].get("version") in (2, 3, 4) and manuscript.get("status") == "assessed"
                and not manuscript.get("claims")):
            return False
    if snapshot.get("figure_methods") and record.get("verdict") == "accept":
        origins = (record.get("checks", {}).get("visual", {}).get("series_origin") or {})
        return set(origins) == set(snapshot["figure_methods"]) and all(
            value in ("measured", "theoretical") for value in origins.values())
    return True


def _delivery_inputs(snapshot):
    # Page locators and blind-review instructions describe how to review the inputs.
    # The scientific files, worker provenance, venue and required checks identify a delivery.
    return {k: v for k, v in snapshot.items() if k not in ("project", "revision", "conclusion", "evidence_scope", "registered_design")}


def delivery_inputs_digest(snapshot):
    return _digest(_canonical(_delivery_inputs(snapshot)))


def delivered_record_matches(snapshot, record, revision, inputs_digest=None):
    """Can an already completed delivery retain its original signature after a bundle upgrade?"""
    if (not isinstance(record, dict) or record.get("revision") != revision or not revision
            or record.get("version") != REVIEW_VERSION or record.get("deliverable") is not True
            or record.get("verdict") != "accept"):
        return False
    if snapshot["revision"] == revision:
        return record_is_current(snapshot, record)

    hashes = {p: meta["sha256"] for p, meta in snapshot["artifacts"].items()}
    pdf = hashes.get(_submission(snapshot)[1])
    if (not pdf or record.get("paper_pdf_sha256") != pdf or record.get("artifact_hashes") != hashes
            or snapshot.get("worker_measurement", {}).get("verified") is False):
        return False
    inputs = _delivery_inputs(snapshot)
    digest = _digest(_canonical(inputs))
    if inputs_digest is not None:
        return digest == inputs_digest

    original_scope = snapshot.get("evidence_scope", {})
    for added_selectors in (NUMERIC_SELECTORS, SUPERIORITY_SELECTORS):
        if not any(key in original_scope for key in added_selectors):
            continue
        # Existing signatures describe the policy used when the review was signed.
        prior = {k: v for k, v in snapshot.items() if k not in ("project", "revision")}
        prior["evidence_scope"] = {k: v for k, v in original_scope.items() if k not in added_selectors}
        if delivered_record_matches({**prior, "revision":_digest(_canonical(prior))}, record, revision):
            return True
    if "recheck_paragraphs" in original_scope:
        older = {k: v for k, v in snapshot.items() if k not in ("project", "revision")}
        policy = original_scope["recheck_paragraphs"]
        if isinstance(policy, dict) and policy.get("version") == 3:
            older["evidence_scope"] = {**original_scope, "recheck_paragraphs":{**policy, "version":2}}
            if _digest(_canonical(older)) == revision:
                return True
        older["evidence_scope"] = {**original_scope, "recheck_paragraphs":True}
        if _digest(_canonical(older)) == revision:
            return True
        older["evidence_scope"] = {k:v for k,v in original_scope.items() if k != "recheck_paragraphs"}
        if _digest(_canonical(older)) == revision:
            return True
    if any(key in original_scope for key in COMPARISON_SELECTORS):
        prior = {k: v for k, v in snapshot.items() if k not in ("project", "revision")}
        prior["evidence_scope"] = {k: v for k, v in original_scope.items() if k not in (*COMPARISON_SELECTORS, "recheck_paragraphs")}
        if _digest(_canonical(prior)) == revision:
            return True
    # Historical receipts have only the hash of their original manifest. Reconstruct
    # published shapes so that a venue, worker or file change still fails comparison.
    if digest == revision:  # alpha.4
        return True
    conclusion = snapshot.get("conclusion")
    if conclusion:
        if scope := snapshot.get("evidence_scope"):
            previous_scope = {key: value for key, value in scope.items() if key not in ("conclusion_clauses", "recheck_paragraphs", *COMPARISON_SELECTORS)}
            for previous_version in (2, 3, 4):
                old_claims = {**inputs, "conclusion":conclusion, "evidence_scope":{**previous_scope, "version":previous_version}}
                if _digest(_canonical(old_claims)) == revision:
                    return True
            old = {**inputs, "conclusion": conclusion, "evidence_scope":{"version":1, "tables":scope["tables"]}}
            if _digest(_canonical(old)) == revision:
                return True
        for locator in (conclusion, {k: v for k, v in conclusion.items() if k != "version"}):
            if _digest(_canonical({**inputs, "conclusion": locator})) == revision:
                return True
    early = {k: inputs[k] for k in ("version", "paper_dir", "target_track", "required", "artifacts")}
    # alpha.2 and .3 catalogued every result file as a measurement.
    early["artifacts"] = {p: ({**meta, "kind":"measurement"} if meta["kind"] == "supporting_measurement" else meta)
                          for p, meta in inputs["artifacts"].items()}
    return _digest(_canonical(early)) == revision


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
