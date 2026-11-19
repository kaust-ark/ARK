"""Lightweight validator for ``auto_research/state/findings.yaml``.

Goals:

- **Backward compatible.** An existing findings file without the new
  ``source:`` / ``construct:`` fields produces warnings, not errors.
  No legacy file gets rejected by this validator being introduced.
- **Additive schema.** We do not replace the free-form dict; we add
  two optional fields that become required only where a number is
  being asserted (``metrics:`` present on a finding).
- **File-reference verification.** If ``source:`` is filled, we check
  the referenced path exists relative to the project dir. Optional
  ``metric_sources`` pin individual numeric measurements to JSON Pointers
  in raw results; a contradictory literal is reported as a blocking issue.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from functools import lru_cache
from itertools import islice
from math import floor, isfinite, ulp
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

import yaml


# ``results/phase3/run07_skills/tpr_fpr.json#tpr``  (path#fragment)
# ``results/phase3/run07_skills/tpr_fpr.json:42``   (path:line)
_SOURCE_RE = re.compile(
    r"^(?P<path>[A-Za-z0-9_./\-]+)"
    r"(?:#(?P<frag>[A-Za-z0-9_.\-\[\]]+|/[^\s#]*)|:(?P<line>\d+(?:-\d+)?))?$"
)


@dataclass
class Violation:
    finding_id: str
    field: str            # "source" | "construct" | "metrics" | "structure"
    severity: str         # "warn" | "block"
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_findings_text(text: str) -> Optional[Dict[str, Any]]:
    try:
        loaded = yaml.safe_load(text) or {}
    except (yaml.YAMLError, ValueError, RecursionError):
        return None
    return loaded if isinstance(loaded, dict) else None


# ────────────────────────────────────────────────────────────────────────
#  Auto-repair for the most common LLM-emitted YAML mistake
# ────────────────────────────────────────────────────────────────────────
#
# Symptom: agent finishes a `findings:` list, then writes a sibling
# top-level key (`sanity_report:`, `coverage:`, `notes:`, ...) but
# leaves it indented at the list-item column instead of dedenting to
# column 0. PyYAML reports
#
#     expected <block end>, but found '?'
#
# at the offending line. This is the dominant source of the
# "findings.yaml is malformed" warnings in production runs because
# every LLM that has tried to extend findings.yaml has eventually made
# this mistake at least once.
#
# The repair is conservative: only rewrite lines that look exactly
# like ``  <identifier>:`` (two-space indent + bare identifier + colon
# + nothing else on the line, possibly trailing whitespace) AND that
# fall within or after a ``findings:`` list scope. Anything more
# ambiguous is left alone — we'd rather log a malformed file than
# corrupt an intentional structure.

_BARE_KEY_AT_2SPACES = re.compile(r'^( {2})([a-z][a-z0-9_]*):\s*$')

# Top-level identifiers we know are intended siblings of `findings:`
# in ARK's findings.yaml schema. This guards against accidentally
# dedenting an unrelated nested key that happens to match the regex.
_KNOWN_TOP_LEVEL_KEYS = frozenset({
    "findings",
    "coverage",
    "sanity_report",
    "surprises",
    "notes",
    "open_questions",
    "carryforward",
    "method_honesty_log",
})


def _dq(scalar: str) -> str:
    """Double-quote a scalar, escaping backslashes and quotes so the result is
    a valid YAML double-quoted string."""
    return '"' + scalar.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _is_quoted(scalar: str) -> bool:
    s = scalar.strip()
    return len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"')


# ────────────────────────────────────────────────────────────────────────
#  Second auto-repair: unquoted scalars containing an inner ``: ``
# ────────────────────────────────────────────────────────────────────────
#
# Symptom: an agent writes a mapping key (or value) that itself contains a
# colon-space, e.g.
#
#     coverage:
#       Experiment 1: Framework Design & Initial Eval:   <- intended as a KEY
#         status: deferred
#
# PyYAML reads `Experiment 1` as the key, opens a value, then hits the second
# `:` and raises ScannerError "mapping values are not allowed here". The fix is
# to wrap the offending scalar in double quotes. We drive the repair off the
# parser's own ``problem_mark`` so only the genuinely failing line is touched,
# and re-parse after each fix (bounded) to walk a file that repeats the mistake.

def _repair_unquoted_colon(text: str) -> tuple[Optional[str], list[str]]:
    """Quote scalars whose inner ``: `` makes YAML raise "mapping values are
    not allowed here". Returns ``(repaired_or_None, changes)``; never raises."""
    changes: list[str] = []
    cur = text
    for _ in range(25):  # bounded: each pass fixes one offending line
        try:
            yaml.safe_load(cur)
            return (cur, changes) if changes else (None, [])
        except yaml.YAMLError as e:
            problem = getattr(e, "problem", "") or ""
            mark = getattr(e, "problem_mark", None)
            if mark is None or "mapping values are not allowed" not in problem:
                break
            lines = cur.split("\n")
            i = mark.line
            if not (0 <= i < len(lines)):
                break
            line = lines[i]
            fixed: Optional[str] = None
            # Case 1: an indented scalar ending in ':' is meant to be a mapping
            # KEY but contains an inner colon — quote the whole key.
            m = re.match(r'^(\s*)(.*\S)\s*:\s*$', line)
            if m and ":" in m.group(2) and not _is_quoted(m.group(2)):
                fixed = f"{m.group(1)}{_dq(m.group(2))}:"
            else:
                # Case 2: `key: value : with colon` — quote the VALUE.
                m2 = re.match(r'^(\s*)([^:#\n]+):\s+(.*\S)\s*$', line)
                if m2 and ":" in m2.group(3) and not _is_quoted(m2.group(3)):
                    fixed = f"{m2.group(1)}{m2.group(2)}: {_dq(m2.group(3))}"
            if not fixed or fixed == line:
                break
            lines[i] = fixed
            changes.append(
                f"line {i+1}: quoted a scalar whose inner ':' YAML "
                f"misread as a nested mapping"
            )
            cur = "\n".join(lines)
    return (None, changes)


def attempt_repair(text: str) -> tuple[Optional[str], list[str]]:
    """Best-effort fix for the common indent-of-sibling-key error.

    Returns ``(repaired_text_or_None, log_messages)``. ``None`` is
    returned when the text already parses cleanly OR when the heuristic
    cannot find a confident repair. ``log_messages`` describes what was
    changed so the caller can surface it. The function never raises.

    Behaviour:
    1. If the text already parses, return ``(None, [])``.
    2. Otherwise, attempt one or more targeted dedents and re-parse.
       Only dedent ``  <identifier>:`` lines whose identifier is in
       ``_KNOWN_TOP_LEVEL_KEYS`` and that appear *after* the first
       ``findings:`` list scope (or after another known top-level
       sibling). All other malformed YAML shapes are left alone.
    3. If the repaired text parses, return it. Otherwise return
       ``(None, [...])`` with diagnostics.
    """
    try:
        yaml.safe_load(text)
        return None, []
    except yaml.YAMLError:
        pass

    lines = text.split("\n")
    repaired = list(lines)
    changes: list[str] = []

    # Find scope boundaries — track when we're inside the `findings:`
    # list (or another top-level mapping) so we can be confident a
    # 2-space `<known_key>:` is genuinely a misplaced sibling.
    inside_findings_list = False
    for i, line in enumerate(lines):
        # Top-level key starts a new scope and ends `findings:`.
        if re.match(r'^[a-z][a-z0-9_]*:\s*$', line):
            inside_findings_list = (line.rstrip(": \t") == "findings")
            continue

        m = _BARE_KEY_AT_2SPACES.match(line)
        if not m:
            continue
        key = m.group(2)
        if key not in _KNOWN_TOP_LEVEL_KEYS:
            continue
        # Heuristic: only repair if we're currently inside the
        # findings list (or otherwise after a known top-level scope
        # — i.e. the only safe time to "dedent to top level"). If we
        # see a 2-space `<known_key>:` while NOT in findings list,
        # we assume the agent meant a nested key and skip.
        if not inside_findings_list:
            continue
        repaired[i] = f"{key}:"
        changes.append(
            f"line {i+1}: dedented misplaced top-level key "
            f"`{key}:` from 2-space to column 0"
        )
        # Once dedented, we are now in the new top-level scope.
        inside_findings_list = False

    # If the indent-dedent pass already makes the text parse, return it.
    # Otherwise fall through to the colon-quoting heuristic, operating on the
    # partially-repaired text so the two fixes can compose.
    repaired_text = "\n".join(repaired)
    if changes:
        try:
            yaml.safe_load(repaired_text)
            return repaired_text, changes
        except yaml.YAMLError:
            pass  # keep the dedents; the file still needs the colon fix

    colon_fixed, colon_changes = _repair_unquoted_colon(repaired_text)
    if colon_fixed is not None:
        return colon_fixed, [*changes, *colon_changes]

    all_changes = [*changes, *colon_changes]
    if not all_changes:
        return None, ["YAML parse error did not match the known indent-misplacement pattern"]

    # We applied at least one targeted repair but the file still doesn't parse
    # (e.g. it also contains an unrelated error). Surface that, preserving the
    # long-standing "still malformed" diagnostic that callers/tests rely on.
    try:
        yaml.safe_load(repaired_text)
    except yaml.YAMLError as e:
        all_changes.append(
            f"After {len(all_changes)} repair(s), YAML still malformed: "
            f"{e.__class__.__name__}"
        )
    return None, all_changes


# ────────────────────────────────────────────────────────────────────────
#  Prevention-at-source: agent authors findings.json → ARK owns findings.yaml
# ────────────────────────────────────────────────────────────────────────
#
# The dominant "findings.yaml is malformed" warnings all stem from an LLM
# hand-writing indentation-sensitive YAML with a generic text editor (sibling
# key left indented inside the `findings:` list; a scalar with an inner `: `
# misread as a nested mapping). Both are structurally impossible in JSON.
#
# So the experimenter/planner now write ``findings.json`` and ARK deterministically
# regenerates the canonical ``findings.yaml`` from it via ``yaml.safe_dump`` — the
# YAML on disk is well-formed by construction. ``findings.yaml`` remains the
# canonical read artifact for every downstream consumer (planner, reviewer,
# summary, state publishing), so nothing on the read side changes.

_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_LINE_COMMENT_RE = re.compile(r'^\s*//.*$', re.MULTILINE)


def _atomic_write_yaml(path: Path, data: Any) -> None:
    """Write ``data`` as canonical YAML atomically (tmp + fsync + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _parse_findings_json(text: str) -> Optional[Any]:
    """Parse JSON, tolerating the two slips LLMs make even in JSON mode:
    a trailing comma before ``}``/``]`` and ``//`` line comments. Returns the
    parsed object, or ``None`` if it still doesn't parse. Never raises."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    cleaned = _LINE_COMMENT_RE.sub("", text)
    cleaned = _TRAILING_COMMA_RE.sub(r"\1", cleaned)
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None


def sync_findings_from_json(state_dir: Path) -> tuple[bool, List[str]]:
    """Regenerate ``findings.yaml`` from an agent-authored ``findings.json``.

    Returns ``(converted, log_messages)``:
    - ``findings.json`` absent → ``(False, [])`` (legacy path; the on-disk
      ``findings.yaml``, if any, is left untouched and the YAML repair path
      in the readers stays as the backstop).
    - ``findings.json`` present and parseable → rewrite ``findings.yaml``
      canonically and return ``(True, [...])``.
    - ``findings.json`` present but unparseable → **do not clobber** any
      existing ``findings.yaml``; return ``(False, [diagnostic])``.

    Never raises — a broken findings artifact must not crash the pipeline.
    """
    state_dir = Path(state_dir)
    json_path = state_dir / "findings.json"
    yaml_path = state_dir / "findings.yaml"
    if not json_path.exists():
        return False, []
    try:
        text = json_path.read_text(encoding="utf-8")
    except OSError as e:
        return False, [f"findings.json unreadable: {e}"]
    if not text.strip():
        return False, []

    data = _parse_findings_json(text)
    if data is None:
        return False, [
            "findings.json is not valid JSON; kept existing findings.yaml "
            "(if any) untouched rather than clobbering it"
        ]
    try:
        _atomic_write_yaml(yaml_path, data)
    except Exception as e:  # noqa: BLE001 — never let serialization crash the run
        return False, [f"failed to write findings.yaml from findings.json: {e}"]
    return True, ["regenerated findings.yaml from agent-authored findings.json"]


def validate_findings(
    findings_path: Path,
    project_root: Optional[Path] = None,
    *,
    require_result: bool = False,
) -> List[Violation]:
    """Validate ``findings.yaml`` at the given path.

    - With ``require_result``, require a nonempty observation for analysis.
      Otherwise absent or empty findings keep their historical audit behavior.
    - Optional per-metric JSON Pointers are verified against measured values.
    - YAML parse error → one ``structure`` violation.
    - Each finding with ``metrics:`` non-empty must have ``source:`` and
      ``construct:`` fields. Missing → ``warn``.
    - ``source:`` value must parse; if its path component is set and
      ``project_root`` is given, the path must exist → ``warn`` on
      dangling.
    """
    findings_path = Path(findings_path)
    missing = [Violation("<root>", "findings", "block",
                         "Record at least one observation in findings.json with a nonempty result.")]
    try:
        if project_root is not None and not findings_path.resolve().is_relative_to(project_root.resolve()):
            return [Violation("<root>", "structure", "block", "Findings must be inside the project.")]
        text = findings_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, RuntimeError):
        return missing if require_result else []
    if not text.strip():
        return missing if require_result else []

    data = _parse_findings_text(text)
    if data is None:
        return [Violation(
            finding_id="<root>",
            field="structure",
            severity="block",
            message=f"{findings_path} is not valid YAML",
        )]

    findings = data.get("findings", []) or []
    violations: List[Violation] = []
    if not isinstance(findings, list):
        violations.append(Violation(
            finding_id="<root>",
            field="structure",
            severity="block",
            message="`findings:` must be a list",
        ))
        return violations

    if require_result and not any(
        isinstance(item, dict) and isinstance(item.get("result"), str) and item["result"].strip()
        for item in findings
    ):
        violations.extend(missing)

    # Multiple headline metrics often cite the same raw file. Keep at most one
    # bounded document in memory, scoped to this audit so a repair sees new data.
    read_result = lru_cache(maxsize=1)(_read_result)
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            violations.append(Violation(
                finding_id=f"[{i}]",
                field="structure",
                severity="warn",
                message="finding entry is not a mapping",
            ))
            continue
        fid = str(f.get("id", f"[{i}]"))
        metrics = f.get("metrics")
        if "metric_sources" in f:
            sources = f["metric_sources"]
            if not isinstance(sources, dict):
                violations.append(Violation(fid, "metric_sources", "block",
                                            "Map measured metric names to results/file.json#/field."))
            else:
                for key, ref in sources.items():
                    problem = _check_metric_source(key, ref, metrics, project_root, read_result)
                    if problem:
                        severity, message = problem
                        violations.append(Violation(fid, f"metric_sources.{key}", severity, message))
        has_metrics = bool(metrics) and (
            not isinstance(metrics, dict) or len(metrics) > 0
        )
        if not has_metrics:
            # Descriptive findings without numbers are fine as-is.
            continue

        source = f.get("source")
        construct = f.get("construct")
        if not source:
            violations.append(Violation(
                finding_id=fid,
                field="source",
                severity="warn",
                message=(
                    "finding asserts metrics but has no `source:` "
                    "(expected file#field or file:line pointing at raw results)"
                ),
            ))
        else:
            violations.extend(_check_source_value(fid, source, project_root))
        if not construct:
            violations.append(Violation(
                finding_id=fid,
                field="construct",
                severity="warn",
                message=(
                    "finding asserts metrics but has no `construct:` "
                    "(describe denominator / population / what is being measured)"
                ),
            ))

    return violations


def _check_source_value(
    fid: str,
    source: Any,
    project_root: Optional[Path],
) -> List[Violation]:
    if not isinstance(source, str):
        return [Violation(
            finding_id=fid,
            field="source",
            severity="warn",
            message=f"`source:` must be a string, got {type(source).__name__}",
        )]
    m = _SOURCE_RE.match(source.strip())
    if not m:
        return [Violation(
            finding_id=fid,
            field="source",
            severity="warn",
            message=(
                f"`source: {source!r}` doesn't match expected "
                "`path[#field|:line]` format"
            ),
        )]
    if project_root is None:
        return []
    path_part = m.group("path")
    relative = Path(path_part)
    try:
        root = project_root.resolve()
        if (relative.is_absolute() or ".." in relative.parts
                or not (root / relative).resolve().is_relative_to(root)):
            return [Violation(fid, "source", "warn", "Use a source path inside the project.")]
        exists = (root / relative).is_file()
    except (OSError, RuntimeError):
        exists = False
    if not exists:
        return [Violation(
            finding_id=fid,
            field="source",
            severity="warn",
            message=(
                f"`source:` path `{path_part}` does not exist under "
                f"{project_root} — provenance looks fabricated"
            ),
        )]
    return []


# Small scalar measurements should usually be in a summary artifact. Large raw
# datasets remain legitimate sources; skip the exact comparison with a warning.
_MAX_RESULT_BYTES = 4 * 1024 * 1024


def _read_result(path: Path):
    """Read once per adjacent source, including failures for a shared artifact."""
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return None, ("block", "must be a regular JSON result file.")
        if info.st_size <= _MAX_RESULT_BYTES:
            with path.open("rb") as stream:
                payload = stream.read(_MAX_RESULT_BYTES + 1)
            if len(payload) <= _MAX_RESULT_BYTES:
                return json.loads(payload, parse_float=Decimal), None
        return None, ("warn", "exceeds the exact-check size limit; cite a smaller result summary.")
    except FileNotFoundError:
        return None, ("block", "does not exist.")
    except (ValueError, InvalidOperation, RecursionError):
        return None, ("block", "could not be parsed as a JSON result.")
    except OSError:
        return None, ("warn", "could not be read for an exact check.")


def _json_pointer(document, pointer):
    for token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise ValueError("invalid JSON Pointer escape")
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(document, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", key):
                raise KeyError(key)
            document = document[int(key)]
        elif isinstance(document, dict):
            document = document[key]
        else:
            raise KeyError(key)
    return document


def _locate_metric_source(ref, project_root):
    """Return the resolved file and decoded JSON Pointer, or an audit issue."""
    filename, _, pointer = ref.partition("#") if isinstance(ref, str) else ("", "", "")
    # Projects may cite CSV rows and scheduler diagnostics as supporting
    # provenance. Existence can be checked; mark the value as unverified.
    line_ref = re.fullmatch(r"(results/[^:#\n]+?\.(?:csv|tsv|txt|log|err|out))"
                            r"(?:\s*\(?line\s+\d+.*|:\d+.*)", filename, re.IGNORECASE)
    relative = Path(line_ref[1] if line_ref else filename)
    if relative.parts[:1] != ("results",) or ".." in relative.parts:
        return None, ("block", "Use a project-relative results/file.json#/field JSON Pointer.")
    if not line_ref and (relative.suffix.lower() != ".json" or not pointer.startswith("/")):
        return None, ("block", "Use a project-relative results/file.json#/field JSON Pointer.")
    if project_root is None:
        return None, None
    try:
        root = project_root.resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root / "results"):
            return None, ("block", "The referenced file is outside the project's results directory.")
        if line_ref:
            if not path.is_file():
                return None, ("block", f"{relative} does not exist as a regular file.")
            return None, ("warn", f"{ref} cites a text artifact; the scalar value was not independently verified.")
        return (path, unquote(pointer, errors="strict")), None
    except (OSError, RuntimeError, ValueError):
        return None, ("block", "Provide a valid path inside the project's results directory.")


def _read_metric_source(location, ref, read_result):
    path, pointer = location
    document, problem = read_result(path)
    if problem:
        severity, detail = problem
        return None, (severity, f"{ref} {detail}")
    try:
        return _json_pointer(document, pointer), None
    except (KeyError, IndexError, ValueError):
        return None, ("block", f"JSON Pointer {ref} was not found.")


def _check_metric_source(key, ref, metrics, project_root, read_result):
    if not isinstance(metrics, dict) or type(metrics.get(key)) not in (str, bool, int, float, Decimal):
        return "block", "Reference a scalar value from the same finding's metrics."
    location, problem = _locate_metric_source(ref, project_root)
    if problem or location is None:
        return problem
    actual, problem = _read_metric_source(location, ref, read_result)
    if problem:
        return problem
    reported = metrics[key]
    if type(reported) in (str, bool):
        if type(actual) is type(reported) and actual == reported:
            return None
        return "block", f"The metadata at {ref} differs from the reported {key}."
    if type(actual) not in (int, Decimal) or not Decimal(actual).is_finite():
        return "block", f"{ref} must point to a finite numeric measurement."
    if actual != Decimal(str(metrics[key])):
        return "block", f"{ref} contains {actual}, but the finding reports {metrics[key]}."
    return None


def _comparison_number(value):
    if type(value) not in (int, float, Decimal):
        return None
    number = Decimal(str(value)) if type(value) is float else Decimal(value)
    if not number.is_finite():
        return None
    # Fraction expands decimal exponents into integers. A short JSON token
    # such as 1e999999999 must not allocate hundreds of MB to check a claim.
    parts = number.as_tuple()
    if abs(parts.exponent) > 4096 or len(parts.digits) > 4096:
        return None
    return Fraction(number)


def _check_numeric_prediction(check, project_root, *, derived):
    if not isinstance(check.get("claim"), str) or not check["claim"].strip():
        return "block", "Identify the finding that the measurements are intended to check."
    operation = check.get("operation")
    if derived:
        if operation not in ("difference", "relative_change_pct", "ratio"):
            return "block", "A derived check requires operation difference, relative_change_pct, or ratio."
        if "observed_source" in check:
            return "block", "Cite both raw operands separately for this comparison; remove observed_source."
    predicted = _comparison_number(check.get("predicted"))
    if predicted is None:
        return "block", "The check requires a finite numeric predicted value within the exact-check limit."
    places = check.get("decimal_places")
    if "decimal_places" in check and (type(places) is not int or not 0 <= places <= 100):
        return "block", "decimal_places must be an integer from 0 through 100 for a rounded claim."

    read_result = lru_cache(maxsize=1)(_read_result)
    operands = []
    binary64_input = False
    locations = []
    fields = ("reference_source", "treatment_source") if derived else ("observed_source",)
    for field in fields:
        ref = check.get(field)
        location, problem = _locate_metric_source(ref, project_root)
        if location is None:
            return "block", f"{field}: {problem[1] if problem else 'Cannot resolve the result file.'}"
        if location in locations:
            return "block", "Reference and treatment must cite distinct raw observations."
        value, problem = _read_metric_source(location, ref, read_result)
        if problem:
            return "block", f"{field}: {problem[1]}"
        operand = _comparison_number(value)
        if operand is None:
            return "block", f"{field} {ref} must point to a finite numeric measurement within the exact-check limit."
        operands.append(operand)
        # Summaries may store separately averaged binary64 statistics. Their
        # printed operands and paired delta can differ in the last bit.
        if (isinstance(value, Decimal) and 15 <= len(value.as_tuple().digits) <= 17
                and Decimal(str(float(value))) == value):
            binary64_input = True
        locations.append(location)

    if derived:
        reference, treatment = operands
        if operation == "difference":
            observed = treatment - reference
        elif reference == 0:
            return "block", f"{operation} is undefined for a zero reference measurement."
        elif operation == "relative_change_pct":
            observed = 100 * (treatment - reference) / reference
        else:
            observed = treatment / reference
    else:
        observed = operands[0]

    if places is not None:
        # Round the exact fraction to the stated decimal place, ties away
        # from zero, including for negative changes and recurring ratios.
        scale = 10 ** places
        rounded = floor(abs(observed) * scale + Fraction(1, 2))
        observed = Fraction(-rounded if observed < 0 else rounded, scale)
    if (derived and places is None and binary64_input and observed != predicted
            and (observed > 0 and predicted > 0 or observed < 0 and predicted < 0)):
        reported = float(Decimal(str(check["predicted"])))
        if (isfinite(reported) and _comparison_number(reported) == predicted
                and abs(observed - predicted) <= Fraction(ulp(reported))):
            return None
    if observed != predicted:
        display = Decimal(observed.numerator) / Decimal(observed.denominator)
        approximation = "approximately " if Fraction(display) != observed else ""
        if not derived:
            return "block", (f"{check['observed_source']} rounded to {places} decimal places gives "
                             f"{approximation}{display}, but the claim reports {check['predicted']}.")
        return "block", (f"Recomputed {operation} gives {approximation}{display} "
                         f"but the claim reports {check['predicted']}. "
                         f"Reference {check['reference_source']}, treatment {check['treatment_source']}. "
                         "For a floating-point aggregate, state the comparison at a meaningful "
                         "precision and use decimal_places, or cite the stored statistic directly.")
    return None


def check_claim_prediction(check: dict, project_root: Path):
    """Compare a predicted scalar or directional comparison with the cited raw results."""
    derived = any(key in check for key in ("operation", "reference_source", "treatment_source"))
    if derived or "decimal_places" in check:
        return _check_numeric_prediction(check, project_root, derived=derived)
    if type(check.get("predicted")) not in (int, float, Decimal):
        return "warn", "Only numeric predictions can be checked against an exact scalar."
    return _check_metric_source("predicted", check.get("observed_source"),
                                {"predicted": check.get("predicted")}, project_root, _read_result)


def analysis_evidence_issues(findings_path: Path, project_root: Path, *, warn=None) -> list[str]:
    """Actionable findings edits before drafting. Only explicit contradictions or
    unusable findings block; uninstrumented legacy metrics remain reviewable.
    """
    issues = validate_findings(findings_path, project_root, require_result=True)
    if warn:
        for v in islice((v for v in issues if v.severity == "warn"), 8):
            warn(f"{v.finding_id}.{v.field}: {v.message}"[:400])
    failures = (f"{v.finding_id}.{v.field}: {v.message}"[:400] for v in issues if v.severity == "block")
    return list(islice(failures, 8))


def format_violations_for_log(violations: List[Violation]) -> str:
    if not violations:
        return ""
    lines = [f"findings.yaml validation: {len(violations)} issue(s)"]
    for v in violations:
        lines.append(f"  [{v.severity}] {v.finding_id}.{v.field}: {v.message}")
    return "\n".join(lines)


_MAX_COMPARISON_CONTROLS = 16
_CONTROL_FIELDS = ("label", "reference_source", "treatment_source")


def _control_locations(control, project_root):
    """Validate a declaration even if the two planned records do not yet exist."""
    if not isinstance(control, dict):
        raise ValueError("Each comparison control must name a label and two result pointers.")
    label = control.get("label")
    if not isinstance(label, str) or not label.strip() or not label.isprintable() or len(label) > 120:
        raise ValueError("Give each comparison control a short, nonempty, single-line label.")
    locations = []
    for field in ("reference_source", "treatment_source"):
        ref = control.get(field)
        if not isinstance(ref, str) or len(ref) > 512 or not ref.isprintable():
            raise ValueError(f"For '{label}', {field} must be a results/file.json#/field pointer.")
        location, problem = _locate_metric_source(ref, project_root)
        if location is None or problem or re.search(r"~(?![01])", location[1]):
            raise ValueError(f"For '{label}', {field} must be a valid JSON Pointer within the project's results directory.")
        locations.append(location)
    if locations[0] == locations[1]:
        raise ValueError(f"For '{label}', reference and treatment must cite distinct raw metadata fields.")
    return locations


def freeze_comparison_controls(plan_path, previous, project_root):
    """Copy bounded pre-run declarations into the existing iteration checkpoint."""
    if (not isinstance(previous, list) or len(previous) > _MAX_COMPARISON_CONTROLS
            or any(not isinstance(c, dict) or not all(k in c for k in _CONTROL_FIELDS) for c in previous)):
        raise ValueError("Invalid saved comparison_controls in the experiment checkpoint.")
    if plan_path is None:
        return list(previous)
    try:
        if (plan_path.resolve().parent != plan_path.parent.resolve()
                or not stat.S_ISREG(plan_path.stat().st_mode)):
            raise ValueError("experiment_plan.yaml must be a regular file in the state directory.")
        with plan_path.open("rb") as stream:
            payload = stream.read(256 * 1024 + 1)
        if len(payload) > 256 * 1024:
            raise ValueError("The experiment plan exceeds the 256 KiB pre-run inspection limit.")
        plan = yaml.safe_load(payload)
    except FileNotFoundError:
        return list(previous)  # Legacy Markdown plans have no structured declarations.
    except (yaml.YAMLError, UnicodeError, OSError, RuntimeError) as exc:
        raise ValueError("Cannot read experiment_plan.yaml to freeze the comparison controls.") from exc
    declared = plan.get("comparison_controls", []) if isinstance(plan, dict) else []
    if not isinstance(declared, list) or len(declared) > _MAX_COMPARISON_CONTROLS:
        raise ValueError(f"comparison_controls must be a list of at most {_MAX_COMPARISON_CONTROLS} comparisons.")
    controls = list(previous)
    for control in declared:
        locations = _control_locations(control, project_root)
        entry = {key: control[key] for key in _CONTROL_FIELDS}
        existing = next((c for c in controls if all(c.get(k) == v for k, v in entry.items())), None)
        if "replaces" in control:
            old_pair = control["replaces"]
            if not isinstance(old_pair, dict) or set(old_pair) != {"reference_source", "treatment_source"}:
                raise ValueError("replaces must specify the exact previous reference_source and treatment_source.")
            if existing and existing.get("replaces") == old_pair:
                continue  # The planner carried an already approved replacement forward.
            old = {"label": entry["label"], **old_pair}
            old_locations = _control_locations(old, project_root)
            if not any(all(c.get(k) == v for k, v in old.items()) for c in previous):
                raise ValueError("A replacement must identify the previously approved pair with the same label.")
            if any(c["label"] == entry["label"] and c.get("replaces") == old_pair for c in controls):
                raise ValueError("This comparison has already been replaced. Reference its current pair.")
            if existing or set(old_locations) == set(locations):
                raise ValueError("A replacement must declare a new pair before the follow-up experiment.")
            entry["replaces"] = dict(old_pair)
            # Preserve what was observed before collecting the replacement.
            # Later file changes cannot retroactively qualify the old pair.
            prior_gaps = comparison_control_gaps([old], project_root)
            if prior_gaps:
                entry["prior_limitation"] = prior_gaps[0]
        if not existing:
            controls.append(entry)
    if len(controls) > _MAX_COMPARISON_CONTROLS:
        raise ValueError(f"The experiment checkpoint supports up to {_MAX_COMPARISON_CONTROLS} declared controls.")
    return controls


def _control_metadata_present(value):
    if isinstance(value, str):
        return bool(value.strip()) and value.strip().casefold() not in {
            "unknown", "n/a", "missing", "not recorded", "unrecorded", "unspecified",
        }
    return type(value) in (int, bool) or (isinstance(value, Decimal) and value.is_finite())


def comparison_control_gaps(controls, project_root, *, include_replaced=False):
    """Check equality of recorded metadata. This cannot authenticate the experiment."""
    if not isinstance(controls, list) or len(controls) > _MAX_COMPARISON_CONTROLS:
        return ["The saved comparison controls are invalid. Omit conclusions requiring a matched comparison."]
    replaced = [{"label": c.get("label"), **c["replaces"]} for c in controls
                if isinstance(c, dict) and isinstance(c.get("replaces"), dict)]
    gaps = []
    read_result = lru_cache(maxsize=1)(_read_result)
    for control in controls:
        if isinstance(control, dict):
            if not include_replaced and {k: control.get(k) for k in _CONTROL_FIELDS} in replaced:
                continue
            if include_replaced and isinstance(control.get("prior_limitation"), str):
                gaps.append(control["prior_limitation"])
        try:
            reference, treatment = _control_locations(control, project_root)
        except ValueError as exc:
            gaps.append(f"Invalid saved comparison control. {exc} Omit conclusions requiring this match.")
            continue
        refs = [control["reference_source"], control["treatment_source"]]
        try:
            if reference[1] == treatment[1] and reference[0].is_file() and treatment[0].is_file():
                if reference[0].samefile(treatment[0]):
                    raise ValueError("Both metadata pointers identify the same raw field.")
            values = []
            for ref, location in zip(refs, (reference, treatment)):
                value, problem = _read_metric_source(location, ref, read_result)
                if problem:
                    raise ValueError(problem[1])
                if not _control_metadata_present(value):
                    raise ValueError(f"{ref} has no recorded scalar metadata value.")
                values.append(value)
            first, second = values
            if first == second and isinstance(first, bool) == isinstance(second, bool):
                continue
            reason = "The recorded metadata values differ."
        except (ValueError, OSError) as exc:
            reason = str(exc)[:250]
        gaps.append(f"Unverified matched control '{control['label']}' ({' and '.join(refs)}). {reason} "
                    "Report these observations separately and omit any attribution that requires this match.")
    return list(dict.fromkeys(gaps))
