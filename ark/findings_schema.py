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
