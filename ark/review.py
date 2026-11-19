"""Read-only, revision-bound evidence and verdicts for an independent reviewer.

Callers own the reviewer session, successful evidence-delivery receipts and
persisted records. Never accept a snapshot, receipt or prior record from the
researcher or from the reviewer model. Only the response is model-authored.
"""
from __future__ import annotations

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


def build_snapshot(project, *, paper_dir="paper", target_track="", required=("manuscript", "experiment")):
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


def read_evidence(snapshot, artifact, *, page=None, start=1, visual=False, statistic=None, column="", rows_pointer=""):
    """Return a bounded text excerpt or an actual rendered PDF page with a receipt."""
    if type(start) is not int or start < 1 or type(visual) is not bool:
        raise ValueError("Invalid evidence offset or mode")
    data, meta = _artifact(snapshot, artifact)
    ext = Path(artifact).suffix.lower()
    receipt = {"revision": snapshot["revision"], "artifact": artifact, "sha256": meta["sha256"], "start": start}
    result = {"artifact": artifact, "sha256": meta["sha256"]}
    if statistic is not None:
        if meta["kind"] != "measurement" or visual or page is not None or start != 1:
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
    if start > max(1, len(text)):
        raise ValueError("Evidence offset is past the end")
    chunk = text[start - 1:start - 1 + VIEW_SIZE]
    receipt["end"] = start - 1 + len(chunk)
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


def review_prompt(snapshot, previous=None):
    instructions = (Path(__file__).parent / "templates/agents/native_reviewer.prompt").read_text()
    tex, pdf = _submission(snapshot)
    catalogue = {p: v["kind"] for p, v in snapshot["artifacts"].items()
                 if Path(p).suffix.lower() not in {".sty", ".bst", ".cls"}}
    packet = {"revision": snapshot["revision"], "target_track": snapshot["target_track"] or "unspecified",
              "required": snapshot["required"], "manuscript_artifact": tex, "rendered_artifact": pdf,
              "manuscript_excerpt": _blind_text(snapshot), "available_artifacts": catalogue}
    if conflicts := reported_change_conflicts(packet["manuscript_excerpt"]):
        packet["reported_changes_to_verify"] = conflicts[:8]
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


def _references(snapshot, refs, observed, blind, *, required):
    if not isinstance(refs, list) or len(refs) > 24 or (required and not refs):
        raise ValueError("Provide specific evidence for this judgment")
    tex, _ = _submission(snapshot)
    verified = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise ValueError("Expected an evidence reference")
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
        receipt_matches = [r for r in observed if isinstance(r, dict) and
                           (r.get("revision"), r.get("artifact"), r.get("sha256"), r.get("page"), r.get("visual", False)) ==
                           (snapshot["revision"], artifact, info["sha256"], page, visual)]
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
            located = view["receipt"] == r and _contains_observed_quote(view["text"], quote)
            if located:
                calculation = options
        if not located:
            raise ValueError(f"Unobserved or unlocatable quote in {artifact}: {quote[:100]}")
        verified.append({"artifact": artifact, **({"page": page} if page else {}),
                         "quote": quote, "sha256": info["sha256"], **calculation})
    return verified


def _covers(snapshot, domain, refs):
    tex, pdf = _submission(snapshot)
    if domain == "manuscript":
        return any(r["artifact"] in (tex, pdf) for r in refs)
    if domain == "visual":
        return any(r["artifact"] == pdf and r.get("visual") for r in refs)
    kind = "measurement" if domain == "experiment" else "primary_source"
    return any(snapshot["artifacts"][r["artifact"]]["kind"] == kind for r in refs)


def _contains_observed_quote(text, quote):
    # PDF extraction can change a printed ligature or wrap a line. Keep numbers,
    # units, words and mathematical relations intact when locating the passage.
    def normalized(value):
        return " ".join(unicodedata.normalize("NFKC", value).replace("\u00ad", "").split())
    return normalized(quote) in normalized(text)


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
    prior_observations = {(r["artifact"], r.get("page"), r.get("quote")) for r in old["evidence"]}
    return any(r["artifact"] not in submission and
               r["sha256"] != previous["artifact_hashes"].get(r["artifact"]) and
               (r["artifact"], r.get("page"), r.get("quote")) not in prior_observations for r in refs)


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
        if assessed and not _covers(snapshot, domain, refs):
            raise ValueError(f"No observed {domain} evidence supports this assessment")
        if assessed and domain == "visual":
            _, pdf = _submission(snapshot)
            import fitz
            with fitz.open(stream=_artifact(snapshot, pdf)[0], filetype="pdf") as doc:
                seen = {r.get("page") for r in observed if r.get("revision") == snapshot["revision"]
                        and r.get("artifact") == pdf and r.get("sha256") == snapshot["artifacts"][pdf]["sha256"]
                        and r.get("visual") is True and r.get("image_sha256")}
                if not set(range(1, len(doc) + 1)).issubset(seen):
                    raise ValueError("Rendered layout was not observed on every manuscript page")
        checks[domain] = {"status": status, "reason": reason, "evidence": refs}

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
    return bool(isinstance(record, dict) and record.get("version") == REVIEW_VERSION
                and record.get("revision") == snapshot["revision"])


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
