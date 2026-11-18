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

import json
import os
import re
from bisect import bisect_left
from decimal import Decimal, DecimalException, localcontext
from pathlib import Path

_COEFFICIENT = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
_VALUE = rf"{_COEFFICIENT}(?:[eE][+-]?\d+)?"
_TEX_EXPONENT = re.compile(
    rf"(?<![\w.\\])({_COEFFICIENT})\s*(?:\\times|\\cdot|×)\s*10\s*\^\s*\{{([+-]?\d+)\}}")
_NUM = re.compile(rf"(?<![\w.\\])({_VALUE})(\s*\\?%)?")
_EVIDENCE_NUMBER = re.compile(_VALUE)
_SI_QUANTITY = re.compile(rf"\\(?:SI|qty)\s*\{{\s*({_VALUE})\s*\}}\s*\{{([^{{}}]+)\}}")
_SECOND_POWERS = {r"\second": 0, r"\milli\second": -3, r"\micro\second": -6, r"\nano\second": -9,
                  "s": 0, "ms": -3, "us": -6, "µs": -6, "ns": -9}
_FIELD_SECONDS = {"seconds": 0, "milliseconds": -3, "microseconds": -6, "nanoseconds": -9,
                  "ms": -3, "us": -6, "ns": -9}
_RESULT_SECTION = re.compile(r"result|experiment|evaluation|analysis|ablation|finding|discussion", re.I)
_SUFFIXES = {".json", ".jsonl", ".csv", ".tsv", ".yaml", ".yml", ".txt", ".md", ".log", ".out"}
_STATE_FILES = ("auto_research/state/findings.yaml", "auto_research/state/findings.json")

MIN_STRONG = 5          # fewer strong numbers than this decides nothing
MIN_TRACED = 0.8        # traced share of strong numbers below this is a finding


def _strip(t: str) -> str:
    t = re.sub(r"(?<!\\)%.*", "", t)
    t = re.sub(r"\\(cite\w*|ref|eqref|autoref|cref|Cref|label|includegraphics|url|href)\*?"
               r"(\[[^\]]*\])*\{[^}]*\}", " ", t)
    t = re.sub(r"\\(begin|end)\{[^}]*\}(\[[^\]]*\])?(\{[^}]*\})?", " ", t)
    t = re.sub(r"\d+(\.\d+)?\s*(pt|cm|mm|in|em|ex)\b", " ", t)
    t = _TEX_EXPONENT.sub(r"\g<1>e\g<2>", t)
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
    return [row[:4] for row in _claims_with_units(tex)]


def _claims_with_units(tex: str) -> list:
    found = []
    for where, body in _regions(tex):
        body = _strip(body)
        units = {m.start(1): m[2].strip() for m in _SI_QUANTITY.finditer(body)}
        for m in _NUM.finditer(body):
            s, pct = m.group(1), bool(m.group(2))
            unit = units.get(m.start(1))
            if unit in (r"\percent", r"\%"):
                pct, unit = True, None
            if "." in s or "e" in s.lower() or pct or unit in _SECOND_POWERS:
                ctx = " ".join(body[max(0, m.start() - 40): m.end() + 20].split())
                found.append((where, s, pct, ctx, unit))
    return found


def _strong(s: str) -> bool:
    coefficient = s.lower().partition("e")[0]
    return len(coefficient.lstrip("+-").replace(".", "").lstrip("0")) >= 3


def _files(root: Path, before, max_files=400, max_depth=3, max_bytes=2_000_000) -> tuple:
    """(files, partial). `partial` means coverage was cut short, not that the
    project is clean: the module's own docstring says projects keep tens of
    thousands of files under results/, and a number living in a file past the
    cap would otherwise be reported as fabricated."""
    out, frontier, partial = [], [(root / "results", 0)], False
    while frontier and len(out) < max_files:
        d, depth = frontier.pop(0)
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            if len(out) >= max_files:
                partial = True
                break
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < max_depth:
                        frontier.append((Path(e.path), depth + 1))
                    else:
                        partial = True          # deeper results never looked at
                elif e.is_file(follow_symlinks=False) and Path(e.name).suffix.lower() in _SUFFIXES:
                    st = e.stat()
                    if st.st_size > max_bytes:
                        partial = True          # a big results file, unread
                    elif before is None or st.st_mtime < before:
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
    return out, (partial or bool(frontier))


def _duration_values(document):
    """Only explicitly dimensioned JSON numbers or arrays of measurements."""
    pending = [document]
    while pending:
        obj = pending.pop()
        if isinstance(obj, list):
            pending.extend(v for v in obj if isinstance(v, (list, dict)))
        elif isinstance(obj, dict):
            for key, value in obj.items():
                power = _FIELD_SECONDS.get(key.lower().rsplit("_", 1)[-1])
                if power is not None:
                    samples = value if isinstance(value, list) else (value,)
                    for item in samples:
                        if isinstance(item, Decimal) and item.is_finite():
                            sign, digits, exponent = item.as_tuple()
                            yield Decimal((sign, digits, exponent + power))
                if isinstance(value, (list, dict)):
                    pending.append(value)


def _index(root: Path, before, need_time: bool, budget=40_000_000) -> tuple:
    values, seconds, read = set(), set(), 0
    files, partial = _files(root, before)
    for p in files:
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        read += len(text)
        if read > budget:
            partial = True
            break
        for m in _EVIDENCE_NUMBER.finditer(text):
            try:
                values.add(Decimal(m.group()))
            except DecimalException:
                continue
        if need_time and p.suffix.lower() in {".json", ".jsonl"}:
            records = text.splitlines() if p.suffix.lower() == ".jsonl" else [text]
            for record in records:
                try:
                    data = json.loads(record, parse_float=Decimal, parse_int=Decimal)
                    seconds.update(_duration_values(data))
                except (ValueError, DecimalException, RecursionError):
                    continue
    return sorted(values), sorted(seconds), partial


def _matches(printed: Decimal, values: list) -> bool:
    """Only the nearest value on each side can round to the printed measurement."""
    position = bisect_left(values, printed)
    for observed in values[max(0, position - 1):position + 1]:
        try:
            if observed.quantize(printed) == printed:
                return True
        except DecimalException:
            continue
    return False


def audit(tex: str, project_dir: Path, before: float | None = None) -> dict:
    """Trace the paper's numbers; `before` limits the evidence to older files."""
    found = _claims_with_units(tex)
    values, seconds, partial = _index(Path(project_dir), before,
                                      need_time=any(row[4] in _SECOND_POWERS for row in found))
    rows = []
    for where, s, pct, ctx, unit in found:
        traced = False
        try:
            printed = Decimal(s)
            with localcontext() as context:
                context.prec = max(context.prec, len(printed.as_tuple().digits))
                if unit in _SECOND_POWERS:
                    traced = _matches(printed.scaleb(_SECOND_POWERS[unit]), seconds)
                elif unit is None:
                    traced = _matches(printed, values)
                    # A table may display 82.31 for an accuracy saved as 0.8231.
                    # Keep that convention for plain decimals, not exponents.
                    if not traced and (pct or ("e" not in s.lower() and 1 <= abs(printed) <= 100)):
                        traced = _matches(printed.scaleb(-2), values)
        except DecimalException:
            pass
        rows.append({"where": where, "number": s + ("%" if pct else ""), "context": ctx,
                     "strong": _strong(s), "traced": traced,
                     "unit_unverified": unit is not None and (unit not in _SECOND_POWERS or not seconds)})
    strong = [r for r in rows if r["strong"]]
    traced = sum(r["traced"] for r in strong)
    missing = sorted((r for r in strong if not r["traced"]), key=lambda row: row["unit_unverified"])
    return {"numbers": len(rows), "strong": len(strong), "strong_traced": traced,
            "ratio": round(traced / len(strong), 2) if strong else None,
            "evidence_values": len(values), "evidence_partial": partial,
            "unit_unverified": sum(r["unit_unverified"] for r in strong),
            "untraced": missing[:10]}


def verdict(result: dict) -> tuple[bool, str]:
    """(ok, detail) for the delivery contract."""
    unit_unknown = result.get("unit_unverified", 0)
    if result["numbers"] >= 10 and result["evidence_values"] == 0:
        if result.get("evidence_partial"):
            # Read nothing because we declined to open it, not because there
            # was nothing there: the caps can skip every result file a project
            # has (one 3 MB jsonl is enough). Same rule as below.
            result["unverified"] = True
            return True, (f"{result['numbers']} result-like numbers in the paper, and the "
                          f"evidence index was CUT SHORT before reading any value, so they "
                          f"are UNVERIFIED rather than unsupported")
        return False, (f"{result['numbers']} result-like numbers in the paper, but results/ "
                       "and findings hold no readable values")
    if result["strong"] >= MIN_STRONG and result["ratio"] < MIN_TRACED:
        examples = "; ".join(f"{r['number']} ({r['context'][:50]})" for r in result["untraced"][:3])
        if result.get("evidence_partial"):
            # The clamp may excuse a number; it must never accuse one. When the
            # index was cut short — too many result files, one too large, or a
            # directory below the depth limit — a number we did not find is a
            # number we did not look for.
            #
            # `unverified` is NOT the same as ok: the contract must report this
            # as "skipped", because "numbers_traced: pass" would put a paper
            # whose evidence was never opened under "all checks passed" — the
            # hole 6576fa0 closed in figures_legible.
            result["unverified"] = True
            return True, (f"{result['strong_traced']}/{result['strong']} numbers traced, and the "
                          f"evidence index was CUT SHORT, so the rest is UNVERIFIED rather than "
                          f"untraced. Not checked against every result file: {examples}")
        checkable = result["strong"] - unit_unknown
        if ((result["strong_traced"] + unit_unknown) / result["strong"] < MIN_TRACED
                or (checkable >= MIN_STRONG and result["strong_traced"] / checkable < MIN_TRACED)):
            return False, (f"only {result['strong_traced']}/{result['strong']} of the paper's numbers "
                           f"appear in results/ or findings. Untraced: {examples}")
    if unit_unknown:
        result["unverified"] = True
        return True, (f"{result['strong_traced']}/{result['strong']} numbers traced. "
                      f"{unit_unknown} use units that could not be checked against unit-labelled measurements.")
    if result["strong"]:
        return True, f"{result['strong_traced']}/{result['strong']} numbers traced to result files"
    return True, "no result numbers to trace"
