"""Do the paper's result numbers come from the project's result files?

Two production papers scored best on numbers no experiment produced. b5fedacd's
7.4 draft printed constants from a synthetic-data fallback in its plotting
script, and 5c0e44f1's 6.7 draft printed per-model accuracies before any model
had been queried. The reviewer is told not to read results/, so nothing in the
loop could see it.

This is the deterministic half of that check. Every result-like number in the
abstract, the tables and the results sections must appear, at the precision
printed, in a file under results/ or in findings. Measured on ten production
runs with the files that existed when each version was written: the two
fabricated drafts traced 0 of 125 and 2 of 99 numbers, while the six verified
final papers traced 96-100%.

Numbers with fewer than three significant digits (0.7, 72%) are counted but do
not decide anything: with thousands of values in results/, they match by
chance.

The check has power only when it runs against the results that exist when the
paper is written or reviewed. Run afterwards against a dense results/, numbers
shifted by 2-3 in their last digit still "trace" at a median rate of 0.91 over
67 production papers, so a pass then is weak evidence. A time-anchored replay
over 158 review-time snapshots flagged 9, of which 7 were synthetic or
unbacked by the reviewers' own account (false-positive rate about 1.3%). It
cannot see synthetic data that code wrote into results/. The walk over results/ is bounded, because some projects keep tens of
thousands of files there on a shared filesystem.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_NUM = re.compile(r"(?<![\w.\\])(-?\d+\.\d+|-?\d+)(\s*\\?%)?")
_RESULT_SECTION = re.compile(r"result|experiment|evaluation|analysis|ablation|finding|discussion", re.I)
_SUFFIXES = {".json", ".jsonl", ".csv", ".tsv", ".yaml", ".yml", ".txt", ".md", ".log", ".out"}
_STATE_FILES = ("auto_research/state/findings.yaml", "auto_research/state/findings.json")

MIN_STRONG = 5          # fewer strong numbers than this decides nothing
MIN_TRACED = 0.8        # traced share of strong numbers below this is a finding
# A number printed at more decimals than this is matched at this many. Both
# sides of the lookup are clamped together, so the clamp can only excuse a
# number, never accuse one.
MAX_PLACES = 8


def _strip(t: str) -> str:
    t = re.sub(r"(?<!\\)%.*", "", t)
    t = re.sub(r"\\(cite\w*|ref|eqref|autoref|cref|Cref|label|includegraphics|url|href)\*?"
               r"(\[[^\]]*\])*\{[^}]*\}", " ", t)
    t = re.sub(r"\\(begin|end)\{[^}]*\}(\[[^\]]*\])?(\{[^}]*\})?", " ", t)
    t = re.sub(r"\d+(\.\d+)?\s*(pt|cm|mm|in|em|ex)\b", " ", t)
    return t.replace("--", " ")          # a range, not a minus sign


def _regions(tex: str) -> list:
    out = []
    m = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", tex, re.S)
    if m:
        out.append(("abstract", m.group(1)))
    out += [("table", m.group(1)) for m in re.finditer(r"\\begin\{table\*?\}(.*?)\\end\{table\*?\}", tex, re.S)]
    parts = re.split(r"(\\section\*?\{[^}]*\})", tex)
    for i in range(1, len(parts) - 1, 2):
        if _RESULT_SECTION.search(parts[i]):
            body = re.sub(r"\\begin\{table\*?\}.*?\\end\{table\*?\}", " ", parts[i + 1], flags=re.S)
            out.append(("section", body))
    return out


def claims(tex: str) -> list:
    """(region, number as printed, is_percent, context) for each decimal or percentage."""
    found = []
    for where, body in _regions(tex):
        body = _strip(body)
        for m in _NUM.finditer(body):
            s, pct = m.group(1), bool(m.group(2))
            if "." in s or pct:
                ctx = " ".join(body[max(0, m.start() - 40): m.end() + 20].split())
                found.append((where, s, pct, ctx))
    return found


def _strong(s: str) -> bool:
    return len(s.lstrip("-").replace(".", "").lstrip("0")) >= 3


def _files(root: Path, before, max_files=400, max_depth=3, max_bytes=2_000_000) -> list:
    out, frontier = [], [(root / "results", 0)]
    while frontier and len(out) < max_files:
        d, depth = frontier.pop(0)
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            if len(out) >= max_files:
                break
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < max_depth:
                        frontier.append((Path(e.path), depth + 1))
                elif e.is_file(follow_symlinks=False) and Path(e.name).suffix.lower() in _SUFFIXES:
                    st = e.stat()
                    if st.st_size <= max_bytes and (before is None or st.st_mtime < before):
                        out.append(Path(e.path))
            except OSError:
                continue
    for name in _STATE_FILES:
        f = root / name
        try:
            if f.is_file() and (before is None or f.stat().st_mtime < before):
                out.append(f)
        except OSError:
            pass
    return out


def _places(s: str) -> int:
    """How many decimals the paper printed, which is the precision to match at."""
    return min(len(s.split(".")[1]), MAX_PLACES) if "." in s else 0


def _index(root: Path, before, places=(0, 1, 2, 3, 4), budget=40_000_000) -> set:
    idx, read = set(), 0
    for p in _files(root, before):
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        read += len(text)
        if read > budget:
            break
        for m in re.finditer(r"-?\d+\.\d+(?:[eE]-?\d+)?|-?\d+", text):
            try:
                v = float(m.group())
            except ValueError:
                continue
            for n in places:
                idx.add(f"{v:.{n}f}")
                idx.add(f"{v * 100:.{n}f}")
    return idx


def audit(tex: str, project_dir: Path, before: float | None = None) -> dict:
    """Trace the paper's numbers; `before` limits the evidence to older files."""
    found = claims(tex)
    # The index must hold the evidence at the precisions the paper prints at, or
    # a number that IS in results/ cannot be found: dsh4 printed an accuracy as
    # 0.93706 while the index stopped at four decimals, so a value sitting in
    # results/raw_results.jsonl was reported as fabricated. The coarse places
    # stay in: a figure rounded for the table still has to trace.
    index = _index(Path(project_dir), before,
                   sorted({0, 1, 2, 3, 4} | {_places(s) for _, s, _, _ in found}))
    rows = []
    for where, s, pct, ctx in found:
        places = _places(s)
        rows.append({"where": where, "number": s + ("%" if pct else ""), "context": ctx,
                     "strong": _strong(s), "traced": f"{float(s):.{places}f}" in index})
    strong = [r for r in rows if r["strong"]]
    traced = sum(r["traced"] for r in strong)
    return {"numbers": len(rows), "strong": len(strong), "strong_traced": traced,
            "ratio": round(traced / len(strong), 2) if strong else None,
            "evidence_values": len(index),
            "untraced": [r for r in strong if not r["traced"]][:10]}


def verdict(result: dict) -> tuple[bool, str]:
    """(ok, detail) for the delivery contract."""
    if result["numbers"] >= 10 and result["evidence_values"] == 0:
        return False, (f"{result['numbers']} result-like numbers in the paper, but results/ "
                       "and findings hold no readable values")
    if result["strong"] >= MIN_STRONG and result["ratio"] < MIN_TRACED:
        examples = "; ".join(f"{r['number']} ({r['context'][:50]})" for r in result["untraced"][:3])
        return False, (f"only {result['strong_traced']}/{result['strong']} of the paper's numbers "
                       f"appear in results/ or findings. Untraced: {examples}")
    if result["strong"]:
        return True, f"{result['strong_traced']}/{result['strong']} numbers traced to result files"
    return True, "no result numbers to trace"
