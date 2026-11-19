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

from ark.artifacts.base import hash_stream
from sqlmodel import select

from .db import Artifact, PendingDecision, ProjectCommand, ProjectMessage

JOURNAL = "auto_research/state/research_events.jsonl"
PAGE_BYTES = 256 * 1024
DATA = {".json", ".csv", ".tsv", ".md", ".txt", ".yaml", ".yml"}
MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pdf": "application/pdf"}


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


def read_record(root, key):
    try:
        with open_project_file(root, key) as f:
            payload = f.read(PAGE_BYTES + 1)
            if len(payload) > PAGE_BYTES:
                return {}
            data = json.loads(payload)
        return data if isinstance(data, dict) else {}
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
        for field in ("count", "total"):
            if type(event.get(field)) is int and event[field] >= 0:
                e[field] = event[field]
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

    if has_pdf:
        add("paper/main.pdf")
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
    return {
        "evidence": list(evidence.values()),
        "milestone": {"text": _text(last.text), "ts": last.created_at.replace(tzinfo=timezone.utc).isoformat()} if last else None,
        "decision_receipt": receipt,
        "command": {"kind": command.kind, "status": command.status} if command else None,
        "review": review_summary(root, review),
    }


@lru_cache(maxsize=256)
def _digest_at_stat(root, key, dev, inode, mtime, ctime, size):
    with open_project_file(root, key) as f:
        return hash_stream(f)[1]


def matches_file(root, key, digest):
    if not re.fullmatch(r"[a-f0-9]{64}", digest or ""):
        return False
    try:
        with open_project_file(root, key) as f:
            st = os.fstat(f.fileno())
        if st.st_size > 64 * 1024 * 1024:
            return False
        return _digest_at_stat(root, key, st.st_dev, st.st_ino, st.st_mtime_ns,
                               st.st_ctime_ns, st.st_size) == digest
    except (OSError, ValueError):
        return False


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def review_summary(root, review):
    revision = _text(review.get("revision"))
    if not re.fullmatch(r"[a-f0-9]{64}", revision):
        return None
    prior = _text(review.get("previous_revision"))
    previous = {}
    if re.fullmatch(r"[a-f0-9]{64}", prior) and prior != revision:
        previous = read_record(root, f"auto_research/state/reviews/{prior}/review.json")
    previous_issues = {row.get("id"): row for row in _rows(previous.get("issues"))}

    def references(issue, version, earlier):
        refs = []
        for ref in _rows(issue.get("evidence"))[:8]:
            key = ref.get("artifact")
            digest = _text(ref.get("sha256"))
            if not evidence_type(key) or not re.fullmatch(r"[a-f0-9]{64}", digest):
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
        row["evidence"] = (references(older, prior, True) if older else []) + references(issue, revision, False)
        issues.append(row)
    stale = not matches_file(root, "paper/main.pdf", _text(review.get("paper_pdf_sha256")))
    # The published PDF can still be the reviewed file while its source is being edited.
    try:
        with open_project_file(root, "auto_research/state/native_review.json") as f:
            reviewed = os.fstat(f.fileno()).st_mtime_ns
        files = review.get("artifact_hashes")
        tracked = [key for key in files if evidence_type(key)] if isinstance(files, dict) else []
        for key in {"paper/main.tex", "paper/references.bib", *tracked[:384]}:
            try:
                with open_project_file(root, key) as f:
                    st = os.fstat(f.fileno())
                    stale |= max(st.st_mtime_ns, st.st_ctime_ns) > reviewed
            except FileNotFoundError:
                stale |= key in tracked
    except (OSError, ValueError):
        stale = True
    checks = review.get("checks")
    coverage = []
    if isinstance(checks, dict):
        for domain in ("manuscript", "experiment", "citations", "visual"):
            check = checks.get(domain)
            if isinstance(check, dict) and check.get("status") in ("assessed", "unassessed"):
                item = {"domain": domain, "status": check["status"], "reason": _text(check.get("reason"))}
                origins = check.get("series_origin")
                hashes = review.get("artifact_hashes")
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
