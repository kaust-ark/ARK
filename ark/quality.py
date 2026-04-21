"""Raw-log sanity checks for experiment results.

Sits between ``run`` phase and ``findings`` phase. Rules scan the raw
files an experiment produced and surface anomalies the summary numbers
may have silently obscured --- e.g. an LLM judge that returned
non-JSON and default-allowed on every call.

Design goals:

- **Narrow starting set.** A noisy scanner gets ignored. Start with two
  rules that have unambiguous true-positive signatures; add more only
  after operating experience.
- **Pluggable.** Rules register via ``@register_rule``; callers compose
  a custom set if needed.
- **Fail-open.** Missing results dir, unreadable files, or unknown
  formats never raise --- they produce no anomaly.
- **Warn vs block.** Default is ``warn`` (surface but don't stop the
  pipeline). A rule can upgrade to ``block`` for catastrophic signals.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, List, Optional


@dataclass
class Anomaly:
    rule_id: str
    severity: str          # "warn" | "block"
    location: str          # file path relative to scanned dir, or "<root>"
    message: str           # human-readable one-liner
    evidence: str          # excerpt from the underlying file
    line_hint: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


SanityRule = Callable[[Path], List[Anomaly]]
_RULES: List[SanityRule] = []


def register_rule(fn: SanityRule) -> SanityRule:
    _RULES.append(fn)
    return fn


def registered_rules() -> List[SanityRule]:
    return list(_RULES)


def raw_log_sanity(
    results_dir: Path,
    rules: Optional[List[SanityRule]] = None,
) -> List[Anomaly]:
    """Scan ``results_dir`` for anomalies. Returns [] if dir is missing."""
    results_dir = Path(results_dir)
    if not results_dir.exists() or not results_dir.is_dir():
        return []
    active = rules if rules is not None else _RULES
    out: List[Anomaly] = []
    for rule in active:
        try:
            out.extend(rule(results_dir))
        except Exception as e:  # a buggy rule must not crash the pipeline
            out.append(Anomaly(
                rule_id=f"{rule.__name__}:internal_error",
                severity="warn",
                location="<sanity-runner>",
                message=f"rule {rule.__name__} raised {type(e).__name__}: {e}",
                evidence="",
            ))
    return out


def format_for_prompt(anomalies: List[Anomaly], max_items: int = 20) -> str:
    """Render anomalies as a block to inject into an agent task message."""
    if not anomalies:
        return ""
    blocking = [a for a in anomalies if a.severity == "block"]
    warnings = [a for a in anomalies if a.severity == "warn"]
    lines: List[str] = [
        "## Raw-Log Sanity Report (auto-generated)",
        "",
        "A machine-audit of `results/` flagged the following anomalies "
        "before you wrote `findings.yaml`. For every metric you report, "
        "address these or explicitly mark them resolved in your "
        "`findings.yaml` under `sanity_report:`. Ignoring a blocking "
        "anomaly invalidates your numbers.",
        "",
    ]
    if blocking:
        lines.append(f"### BLOCKING ({len(blocking)})")
        for a in blocking[:max_items]:
            lines.append(
                f"- **[{a.rule_id}]** `{a.location}`"
                + (f":{a.line_hint}" if a.line_hint else "")
                + f" — {a.message}"
            )
            if a.evidence:
                snippet = a.evidence if len(a.evidence) <= 200 else a.evidence[:200] + "…"
                lines.append(f"    evidence: `{snippet}`")
        lines.append("")
    if warnings:
        shown = warnings[:max_items]
        lines.append(f"### WARN ({len(warnings)})")
        for a in shown:
            lines.append(
                f"- **[{a.rule_id}]** `{a.location}`"
                + (f":{a.line_hint}" if a.line_hint else "")
                + f" — {a.message}"
            )
        if len(warnings) > len(shown):
            lines.append(f"- …and {len(warnings) - len(shown)} more warnings")
        lines.append("")
    return "\n".join(lines)


# ─── Built-in rules ──────────────────────────────────────────────────────
#
# Starting set is intentionally small. Each rule must have a clear,
# unambiguous true-positive signature; false positives train agents to
# ignore the whole report.

_PARSE_FAILURE_PATTERNS = re.compile(
    r"(Expecting value|JSON.*decode|JSONDecodeError|parse.*error|"
    r"LLM.*(exception|escalation exception))",
    re.IGNORECASE,
)


@register_rule
def _rule_judge_parse_failure(results_dir: Path) -> List[Anomaly]:
    """Flag JSONL/JSON files where an LLM judge silently default-allowed.

    Signature: a file looks like per-decision records with a ``reason``
    or ``error`` field, and >50% of those records match a JSON-parse /
    LLM-exception pattern, over at least 10 records.
    """
    out: List[Anomaly] = []
    for path in results_dir.rglob("*.jsonl"):
        try:
            records = []
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue

        # Collect all reason/error text across intercept-shaped records
        reason_texts: List[str] = []

        def _walk(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in ("reason", "error") and isinstance(v, str):
                        reason_texts.append(v)
                    else:
                        _walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    _walk(item)

        for r in records:
            _walk(r)

        if len(reason_texts) < 10:
            continue
        bad = sum(1 for t in reason_texts if _PARSE_FAILURE_PATTERNS.search(t))
        rate = bad / len(reason_texts)
        if rate > 0.5:
            sample = next(
                (t for t in reason_texts if _PARSE_FAILURE_PATTERNS.search(t)),
                "",
            )
            out.append(Anomaly(
                rule_id="judge_parse_failure",
                severity="block",
                location=str(path.relative_to(results_dir)),
                message=(
                    f"{bad}/{len(reason_texts)} decisions "
                    f"({rate:.0%}) have JSON-parse / LLM-exception "
                    "reasons — judge likely default-allowed silently; "
                    "derived TPR/FPR do not reflect judge behaviour"
                ),
                evidence=sample,
            ))
    return out


@register_rule
def _rule_nonzero_stderr(results_dir: Path) -> List[Anomaly]:
    """Flag substantive .err files (slurm stderr, process stderr dumps).

    Signature: a ``.err`` file > 100 bytes whose content mentions a
    known error token. Empty .err is fine; tiny stderr often harmless.
    """
    error_tokens = re.compile(
        r"(Traceback|\bError\b|\bException\b|\bfatal\b|"
        r"CUDA out of memory|Killed|segmentation fault|"
        r"command not found|ModuleNotFoundError)",
        re.IGNORECASE,
    )
    out: List[Anomaly] = []
    for path in results_dir.rglob("*.err"):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < 100:
            continue
        try:
            txt = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = error_tokens.search(txt)
        if not m:
            continue
        snippet = txt[max(0, m.start() - 40): m.end() + 120]
        out.append(Anomaly(
            rule_id="nonzero_stderr",
            severity="warn",
            location=str(path.relative_to(results_dir)),
            message=(
                f"stderr file ({size} B) contains error token "
                f"'{m.group(0)}'; experiment may have failed partway"
            ),
            evidence=snippet.strip(),
        ))
    return out
