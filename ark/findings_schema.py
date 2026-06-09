"""Lightweight validator for ``auto_research/state/findings.yaml``.

Goals:

- **Backward compatible.** An existing findings file without the new
  ``source:`` / ``construct:`` fields produces warnings, not errors.
  No legacy file gets rejected by this validator being introduced.
- **Additive schema.** We do not replace the free-form dict; we add
  two optional fields that become required only where a number is
  being asserted (``metrics:`` present on a finding).
- **File-reference verification.** If ``source:`` is filled, we check
  the referenced path exists relative to the project dir. A dangling
  reference is a strong signal the agent invented a provenance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


# ``results/phase3/run07_skills/tpr_fpr.json#tpr``  (path#fragment)
# ``results/phase3/run07_skills/tpr_fpr.json:42``   (path:line)
_SOURCE_RE = re.compile(
    r"^(?P<path>[A-Za-z0-9_./\-]+)"
    r"(?:#(?P<frag>[A-Za-z0-9_.\-\[\]]+)|:(?P<line>\d+(?:-\d+)?))?$"
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
    except yaml.YAMLError:
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


def validate_findings(
    findings_path: Path,
    project_root: Optional[Path] = None,
) -> List[Violation]:
    """Validate ``findings.yaml`` at the given path.

    - File missing / empty → no violations (nothing to check).
    - YAML parse error → one ``structure`` violation.
    - Each finding with ``metrics:`` non-empty must have ``source:`` and
      ``construct:`` fields. Missing → ``warn``.
    - ``source:`` value must parse; if its path component is set and
      ``project_root`` is given, the path must exist → ``warn`` on
      dangling.
    """
    findings_path = Path(findings_path)
    if not findings_path.exists():
        return []
    try:
        text = findings_path.read_text(encoding="utf-8")
    except OSError:
        return []
    if not text.strip():
        return []

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
    full = (project_root / path_part).resolve()
    try:
        exists = full.exists()
    except OSError:
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


def format_violations_for_log(violations: List[Violation]) -> str:
    if not violations:
        return ""
    lines = [f"findings.yaml validation: {len(violations)} issue(s)"]
    for v in violations:
        lines.append(f"  [{v.severity}] {v.finding_id}.{v.field}: {v.message}")
    return "\n".join(lines)
