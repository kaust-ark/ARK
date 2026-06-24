"""Quality gate for model→artifact boundaries.

Weak models routinely emit deficient artifacts at the points where the pipeline
trusts model output: malformed/empty JSON, a ``needs_human`` signal with no
detail, a figure plan with zero figures, a completeness verdict that doesn't
parse. Rather than patch each site one at a time, every such boundary should
pass model output through one gate:

    parse → validate → (repair once | safe default)

so garbage never propagates downstream and the pipeline degrades *predictably*
instead of surfacing brokenness to the user (a contextless "Agent blocked"
prompt) or silently shipping a deficient artifact (a 1-page paper, no concept
figure).

Dependency-light by design: validators are plain predicates, not a schema lib.

Boundaries wired so far: needs_human.json, completeness evaluation. Next
targets (ad-hoc parsing today): title/paper-list extraction, figure planning
(enforce ≥1 concept figure), decision authoring. See callers in pipeline.py.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional


def extract_json(text: str, *, want: Optional[type] = None) -> Optional[Any]:
    """Best-effort pull of the first JSON value out of LLM text.

    Handles ```json fences and leading/trailing prose by trying the whole
    string, then the widest ``{...}`` and ``[...]`` spans. Returns None if
    nothing parses (or the parsed type isn't ``want``)."""
    if not text:
        return None
    s = text.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", s, re.DOTALL)
    if m:
        s = m.group(1).strip()
    candidates = [s]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = s.find(open_c), s.rfind(close_c)
        if 0 <= i < j:
            candidates.append(s[i:j + 1])
    for c in candidates:
        try:
            v = json.loads(c)
        except Exception:
            continue
        if want is None or isinstance(v, want):
            return v
    return None


def gate(value: Any, checks: list[tuple[str, Callable[[Any], bool]]], *,
         label: str = "artifact", default: Any = None,
         log: Optional[Callable[[str, str], None]] = None,
         repair: Optional[Callable[[], Any]] = None) -> tuple[bool, Any]:
    """Validate ``value`` against ``checks`` (``[(name, predicate), …]``).

    On the first failed predicate: try ``repair()`` once (if given); if the
    repaired value passes, use it; otherwise fall back to ``default``. Returns
    ``(ok, value)`` — ``ok`` is True when the original or repaired value passed.
    ``log(msg, level)`` is invoked on failure/repair (fail-soft)."""
    def _first_failure(v: Any) -> Optional[str]:
        for name, pred in checks:
            try:
                if not pred(v):
                    return name
            except Exception:
                return name
        return None

    bad = _first_failure(value)
    if bad is None:
        return True, value
    if repair is not None:
        try:
            rv = repair()
            if _first_failure(rv) is None:
                if log:
                    log(f"{label}: failed check '{bad}', repaired.", "WARN")
                return True, rv
        except Exception:
            pass
    if log:
        log(f"{label}: failed check '{bad}' — using safe default.", "WARN")
    return False, default


# ── Common predicates ────────────────────────────────────────────────────────
def non_empty_str(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def non_empty_list(v: Any) -> bool:
    return isinstance(v, list) and len(v) > 0


def is_dict(v: Any) -> bool:
    return isinstance(v, dict)


def has_fields(*keys: str) -> Callable[[Any], bool]:
    """Predicate: value is a dict with every key present AND non-empty."""
    def _check(v: Any) -> bool:
        return isinstance(v, dict) and all(
            k in v and v[k] not in (None, "", [], {}) for k in keys
        )
    return _check


def any_field(*keys: str) -> Callable[[Any], bool]:
    """Predicate: value is a dict with at least one key present AND non-empty."""
    def _check(v: Any) -> bool:
        return isinstance(v, dict) and any(
            v.get(k) not in (None, "", [], {}) for k in keys
        )
    return _check
