"""Read the project's confirmed research observations and authorized evidence."""
from __future__ import annotations

import json
import os
import re
import stat
from contextlib import ExitStack
from datetime import timezone
from functools import lru_cache
from pathlib import Path
from time import time_ns

from ark.artifacts.base import hash_stream
from ark.review import MAX_ARTIFACTS, MAX_BYTES
from sqlmodel import select

from .db import Artifact, PendingDecision, ProjectCommand, ProjectMessage

JOURNAL = "auto_research/state/research_events.jsonl"
PAGE_BYTES = 256 * 1024
DATA = {".json", ".csv", ".tsv", ".md", ".txt", ".yaml", ".yml"}
MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pdf": "application/pdf"}
REVIEW_EXCERPT = re.compile(
    r"(?:auto_research/state/(?:idea\.md|native_runs/[a-f0-9]{24}/input/main\.py)"
    r"|(?:scripts|experiments)/(?:[\w-]+/)*[\w.-]+\.(?:py|r|jl|sh))", re.I
)


def open_project_file(root, key):
    """Open a regular file relative to the workspace, without following links."""
    parts = key.split("/")
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise ValueError("invalid project path")
    with ExitStack() as stack:
        dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = os.open(root, dir_flags)
        stack.callback(os.close, parent)
        for part in parts[:-1]:
            parent = os.open(part, dir_flags, dir_fd=parent)
            stack.callback(os.close, parent)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    file = os.fdopen(fd, "rb")
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        file.close()
        raise ValueError("not a regular file")
    return file


def evidence_type(key):
    if not isinstance(key, str) or len(key) > 512 or any(ord(c) < 32 for c in key):
        return None
    if any(p.startswith(".") or not p for p in key.split("/")) or "\\" in key:
        return None
    if re.search(r"(?:^|[/._-])(credentials?|secrets?|tokens?|environment_setup|config)(?:[/._-]|$)", key, re.I):
        return None
    suffix = Path(key).suffix.lower()
    if (key == "paper/main.pdf" or key.startswith("paper/figures/")
            or re.fullmatch(r"auto_research/state/reviews/[a-f0-9]{64}/main\.pdf", key)):
        return MEDIA.get(suffix)
    if key.startswith("sources/") and suffix == ".pdf":
        return MEDIA[suffix]
    if re.fullmatch(r"auto_research/state/reviews/[a-f0-9]{64}/(?:main\.tex|references\.bib)", key):
        return "text/plain"
    if (key in {"auto_research/state/experiment_plan.yaml", "paper/main.tex", "paper/references.bib", "auto_research/state/deep_research.md",
                "auto_research/state/findings.yaml", "auto_research/state/findings.json"}
            or key.startswith(("results/", "sources/")) and suffix in DATA | {".jsonl"}):
        return "text/plain"
    return None


def file_exists(root, key):
    try:
        with open_project_file(root, key) as f:
            return bool(os.fstat(f.fileno()).st_size)
    except (ValueError, OSError):
        return False


def _text(s, limit=600):
    return s[:limit].strip() if isinstance(s, str) else ""


def read_record(root, key, *, missing=None):
    try:
        with open_project_file(root, key) as f:
            size = os.fstat(f.fileno()).st_size
            # A full native review can exceed one journal page (quoted findings,
            # independent controls and hashes of the complete submission).
            if size > 8 * 1024 * 1024:
                return {}
            payload = f.read(size + 1)
            if len(payload) != size:
                return {}
            data = json.loads(payload)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {} if missing is None else missing
    except (OSError, ValueError, RecursionError):
        return {}


def journal(root, after=None, before=None):
    """The byte immediately after the last newline is a reconnect-safe cursor."""
    cursor, events = 0, []
    try:
        with open_project_file(root, JOURNAL) as f:
            size = os.fstat(f.fileno()).st_size
            if before is not None:
                size = max(0, min(before, size))
            cursor = after if after is not None and 0 <= after <= size else max(0, size - PAGE_BYTES)
            if cursor:
                f.seek(cursor - 1)
                if f.read(1) != b"\n":
                    f.readline(PAGE_BYTES)
                    cursor = f.tell()
            data = f.read(min(PAGE_BYTES, max(0, size - cursor)))
    except (OSError, ValueError):
        return 0, []
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break  # The writer may still be appending this record.
        offset = cursor
        cursor += len(line)
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict) or not _text(event.get("id")):
            continue
        e = {k: _text(event.get(k), 640) for k in
             ("id", "ts", "phase", "action", "observation", "status", "revision")}
        e["offset"] = offset
        if e["action"] == "inference_test_finished":
            # Both reviewer-defined labels can be 1,200 characters before the numeric outcomes.
            e["observation"] = _text(event.get("observation"), 3000)
        for field in ("count", "total"):
            if type(event.get(field)) is int and event[field] >= 0:
                e[field] = event[field]
        if e["action"] in ("measurement_reproduction_started", "measurement_reproduction_finished"):
            for field in ("condition", "metric", "reported_value", "independent_value"):
                if isinstance(event.get(field), str):
                    e[field] = _text(event[field], 160)
            for field in ("differed", "outcomes"):
                if type(event.get(field)) is int and 0 <= event[field] <= 16:
                    e[field] = event[field]
        if e["action"] in ("measurement_reproduction_started", "measurement_reproduction_failed"):
            attempt = event.get("attempt")
            if type(attempt) is int and attempt > 0:
                e["attempt"] = attempt
            if e["action"].endswith("_failed") and event.get("failure") in ("row_format", "execution"):
                e["failure"] = event["failure"]
        if evidence_type(event.get("artifact")):
            e["artifact"] = event["artifact"]
        events.append(e)
    return cursor, events if after is not None else events[-32:]


def snapshot(session, project, root, *, manage, has_pdf):
    evidence = {}
    def add(key, label=None):
        media = evidence_type(key)
        if media and key not in evidence:
            evidence[key] = {"key": key, "label": label or Path(key).name, "type": media}

    edition, delivered = paper_edition(root)
    if edition == "delivered" or (has_pdf and edition != "unavailable"):
        add("paper/main.pdf")
        if edition:
            evidence["paper/main.pdf"]["edition"] = edition
        if delivered:
            evidence["paper/main.pdf"]["delivered_sha256"] = delivered["paper_pdf_sha256"]
    if not manage:
        return {"evidence": list(evidence.values())}

    for item in session.exec(select(Artifact).where(
            Artifact.project_id == project.id, Artifact.kind.in_(["figure", "result"]))
            .order_by(Artifact.updated_at.desc(), Artifact.id.desc()).limit(18)).all():
        add(item.key)
    for key in ("results/experiment.json", "auto_research/state/deep_research.md"):
        if file_exists(root, key):
            add(key)
    last = session.exec(select(ProjectMessage).where(
        ProjectMessage.project_id == project.id, ProjectMessage.kind == "milestone"
    ).order_by(ProjectMessage.created_at.desc()).limit(1)).first()
    command = session.exec(select(ProjectCommand).where(
        ProjectCommand.project_id == project.id
    ).order_by(ProjectCommand.created_at.desc()).limit(1)).first()
    decision = session.exec(select(PendingDecision).where(
        PendingDecision.project_id == project.id, PendingDecision.status == "answered"
    ).order_by(PendingDecision.answered_at.desc()).limit(1)).first()
    receipt = None
    if decision:
        ack = session.exec(select(ProjectMessage).where(
            ProjectMessage.project_id == project.id, ProjectMessage.kind == "decision_ack"
        ).order_by(ProjectMessage.created_at.desc()).limit(1)).first()
        try:
            meta = json.loads(ack.meta) if ack else {}
        except ValueError:
            meta = {}
        status = meta.get("status") if isinstance(meta, dict) and meta.get("decision_id") == decision.id else None
        receipt = status if status in ("consumed", "failed") else "recorded"
    review = read_record(root, "auto_research/state/native_review.json")
    working = review_summary(root, review)
    return {
        "evidence": list(evidence.values()),
        "milestone": {"text": _text(last.text), "ts": last.created_at.replace(tzinfo=timezone.utc).isoformat()} if last else None,
        "decision_receipt": receipt,
        "command": {"kind": command.kind, "status": command.status} if command else None,
        "review": review_summary(root, delivered, delivered=True) if delivered else working,
        "working_review": working if delivered and working and (
            working["revision"] != delivered["revision"] or working["stale"]) else None,
        "has_working_pdf": has_pdf,
    }


@lru_cache(maxsize=2048)
def _digest_at_stat(root, key, dev, inode, mtime, ctime, size):
    with open_project_file(root, key) as f:
        st = os.fstat(f.fileno())
        if (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size) != (dev, inode, mtime, ctime, size):
            raise ValueError("Review artifact changed while opening it")
        return hash_stream(f)[1]


def matches_file(root, key, digest):
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        return False
    try:
        with open_project_file(root, key) as f:
            st = os.fstat(f.fileno())
            if st.st_size > MAX_BYTES:
                return False
            # Rapid in-place writes can share inode, size and timestamps on
            # coarse filesystems. Only cache versions older than that window.
            if time_ns() - max(st.st_mtime_ns, st.st_ctime_ns) < 2_000_000_000:
                return hash_stream(f)[1] == digest
        return _digest_at_stat(root, key, st.st_dev, st.st_ino, st.st_mtime_ns,
                               st.st_ctime_ns, st.st_size) == digest
    except (OSError, ValueError):
        return False


def paper_edition(root):
    """Resolve the last completed delivery independently of the active draft."""
    state_key = "auto_research/state/native_project.json"
    state = read_record(root, state_key, missing=False)
    if state is False:
        return None, None  # Legacy project or remote artifact store
    if state and "delivery_revision" not in state:
        return "working", None
    revision = state.get("delivery_revision")
    if isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{64}", revision):
        archive = f"auto_research/state/reviews/{revision}"
        record = read_record(root, f"{archive}/review.json")
        digest = record.get("paper_pdf_sha256")
        hashes = record.get("artifact_hashes")
        if (record.get("revision") == revision and record.get("deliverable") is True
                and record.get("verdict") == "accept" and isinstance(hashes, dict)
                and hashes.get("paper/main.pdf") == digest
                and matches_file(root, f"{archive}/main.pdf", digest)):
            return "delivered", record
    return "unavailable", None


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def review_summary(root, review, *, delivered=False):
    revision = _text(review.get("revision"))
    if not re.fullmatch(r"[a-f0-9]{64}", revision):
        return None
    prior = _text(review.get("previous_revision"))
    previous = {}
    if re.fullmatch(r"[a-f0-9]{64}", prior) and prior != revision:
        previous = read_record(root, f"auto_research/state/reviews/{prior}/review.json")
    previous_issues = {row.get("id"): row for row in _rows(previous.get("issues"))}

    hashes = review.get("artifact_hashes", {})

    def references(entries, version, earlier, *, signed_only=False):
        refs = []
        for ref in _rows(entries)[:24 if signed_only else 8]:
            key = ref.get("artifact")
            digest = _text(ref.get("sha256"))
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                continue
            if signed_only and (not isinstance(key, str) or not isinstance(hashes, dict) or hashes.get(key) != digest):
                continue
            if not evidence_type(key):
                # Show only the passage already recorded by the private reviewer.
                # Source code and the project brief have no download route.
                if isinstance(key, str) and REVIEW_EXCERPT.fullmatch(key) and (quote := _text(ref.get("quote"), 1200)):
                    refs.append({"artifact": key, "previous": earlier, "quote": quote})
                continue
            archived = key
            if key in ("paper/main.pdf", "paper/main.tex", "paper/references.bib"):
                candidate = f"auto_research/state/reviews/{version}/{Path(key).name}"
                if key == "paper/main.pdf" or file_exists(root, candidate):
                    archived = candidate
            refs.append({"artifact": key, "key": archived, "sha256": digest, "previous": earlier,
                         "page": ref.get("page") if type(ref.get("page")) is int else None,
                         "quote": _text(ref.get("quote"), 1200)})
        return refs

    issues = []
    for issue in _rows(review.get("issues"))[:32]:
        row = {k: _text(issue.get(k)) for k in ("id", "claim", "reason", "fix", "status", "severity")}
        older = previous_issues.get(row["id"])
        row["evidence"] = (references(older.get("evidence"), prior, True) if older else []) + references(issue.get("evidence"), revision, False)
        issues.append(row)
    # Private inputs such as split TeX sources and method code also bind the
    # review. Their hashes can be checked without granting download access.
    stale = not delivered and (
        not matches_file(root, "paper/main.pdf", review.get("paper_pdf_sha256"))
        or not isinstance(hashes, dict) or len(hashes) > MAX_ARTIFACTS
        or any(not matches_file(root, key, digest) for key, digest in hashes.items())
    )
    checks = review.get("checks")
    coverage = []
    if isinstance(checks, dict):
        for domain in ("manuscript", "experiment", "citations", "visual"):
            check = checks.get(domain)
            if isinstance(check, dict) and check.get("status") in ("assessed", "unassessed"):
                item = {"domain": domain, "status": check["status"], "reason": _text(check.get("reason"))}
                item["evidence"] = references(check.get("evidence"), revision, False)
                support = check.get("conclusion_support")
                if domain == "manuscript" and check["status"] == "assessed" and support in (
                    "measured", "interpretation", "needs_evidence"
                ):
                    item["conclusion_support"] = support
                if domain == "manuscript" and check["status"] == "assessed":
                    claims = []
                    for claim in _rows(check.get("claims"))[:8]:
                        passage = claim.get("passage")
                        if not isinstance(passage, dict) or passage.get("artifact") != "paper/main.pdf":
                            continue
                        source = references([passage], revision, False, signed_only=True)
                        kind, basis = claim.get("kind"), claim.get("basis")
                        reason = _text(claim.get("reason"), 4000)
                        if (kind in ("result", "explanation") and basis in ("measurement", "control", "derivation", "qualified", "unsupported")
                                and reason and source and source[0]["quote"] and (source[0]["page"] or 0) > 0
                                and source[0]["sha256"] == review.get("paper_pdf_sha256")):
                            claims.append({"kind": kind, "passage": source[0], "basis": basis, "reason": reason,
                                           "evidence": references(claim.get("evidence"), revision, False, signed_only=True)})
                    if claims:
                        item["claims"] = claims
                scope = review.get("evidence_scope")
                if domain == "manuscript" and isinstance(scope, dict):
                    fields = {key: _text(scope.get(key), 1200) for key in ("comparison", "alternative", "discriminator")}
                    if all(fields.values()):
                        item["evidence_scope"] = fields
                origins = check.get("series_origin")
                if domain == "experiment" and isinstance(hashes, dict):
                    controls = []
                    for control in _rows(check.get("controls"))[:48]:
                        key = _text(control.get("artifact"), 512)
                        digest = _text(control.get("sha256"))
                        if (type(control.get("matches")) is not bool or "expected" not in control or "observed" not in control
                                or not (evidence_type(key) or REVIEW_EXCERPT.fullmatch(key))
                                or not re.fullmatch(r"[a-f0-9]{64}", digest) or hashes.get(key) != digest):
                            continue
                        target = _text(control.get("target"), 640)
                        rationale = _text(control.get("rationale"), 1200)
                        if target and rationale:
                            # Keep the recorded numeric precision when JavaScript displays a large integer.
                            item_control = {"target": target, "expected": json.dumps(control["expected"], ensure_ascii=False),
                                             "observed": json.dumps(control["observed"], ensure_ascii=False),
                                             "matches": control["matches"], "rationale": rationale,
                                             "artifact": key, "sha256": digest}
                            if control.get("control") == 2:
                                expected, reported = control["expected"], control["observed"]
                                columns, differences = control.get("columns"), control.get("differences")
                                if (isinstance(columns, list) and 0 < len(columns) <= 16 and isinstance(differences, list)
                                        and isinstance(expected, dict) and isinstance(reported, dict)
                                        and all(isinstance(c, str) and c in expected and c in reported for c in columns)):
                                    item_control["outcomes"] = [{"name": c, "expected": str(expected[c]),
                                        "reported": str(reported[c]), "matches": c not in differences} for c in columns]
                            controls.append(item_control)
                    if controls:
                        item["controls"] = controls
                if domain == "visual" and isinstance(origins, dict):
                    figures = []
                    for name, origin in list(origins.items())[:48]:
                        key = "paper/figures/" + name
                        if "/" in name or not evidence_type(key) or origin not in ("measured", "copied", "theoretical"):
                            continue
                        digest = _text(hashes.get(key)) if isinstance(hashes, dict) else ""
                        figure = {"name": name, "origin": origin}
                        if re.fullmatch(r"[a-f0-9]{64}", digest):
                            figure.update(key=key, sha256=digest)
                        figures.append(figure)
                    item["figures"] = figures
                coverage.append(item)
    return {"revision": revision, "verdict": _text(review.get("verdict")), "checks": coverage,
            "summary": _text(review.get("summary")), "issues": issues, "stale": stale}
