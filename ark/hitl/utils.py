from __future__ import annotations
import json
import re
import yaml
from datetime import datetime
from pathlib import Path

_HITL_OPTION_PAREN_RE = re.compile(
    r"\(([a-z])\)\s*([^()]+?)(?=\(\w\)|$)", re.IGNORECASE,
)
_HITL_TRAILING_CONJUNCTION_RE = re.compile(
    r"(?:[,;.\s]*\b(?:or|and)\b\s*)+$", re.IGNORECASE,
)

_NONBLOCKING_URGENCIES = frozenset({
    "clarification",
    "informational",
    "info",
    "note",
    "advisory",
    "fyi",
    "soft",
})

def _clean_option_title(text: str) -> str:
    s = str(text).strip()
    s = _HITL_TRAILING_CONJUNCTION_RE.sub("", s).rstrip(",;. ")
    return s

def _coerce_hitl_options(raw: dict) -> list:
    """Return a canonical [{id, title, consequence}] list."""
    options = raw.get("options")
    if isinstance(options, list) and options and all(isinstance(o, dict) for o in options):
        return [
            {"id": str(o.get("id") or i),
             "title": str(o.get("title") or "").strip(),
             "consequence": str(o.get("consequence") or "").strip()}
            for i, o in enumerate(options, 1)
        ]

    legacy = raw.get("operator_action_needed") or raw.get("needed_items") or ""
    if isinstance(legacy, list):
        return [
            {"id": str(i),
             "title": _clean_option_title(item),
             "consequence": ""}
            for i, item in enumerate(legacy, 1)
            if str(item).strip()
        ]
    if isinstance(legacy, str) and legacy.strip():
        matches = _HITL_OPTION_PAREN_RE.findall(legacy)
        if matches:
            return [
                {"id": str(idx + 1),
                 "title": _clean_option_title(text),
                 "consequence": ""}
                for idx, (_, text) in enumerate(matches)
            ]
        return [{"id": "1",
                 "title": _clean_option_title(legacy),
                 "consequence": ""}]
    return []

def _extract_hitl_urgency(raw: dict) -> str:
    """Return the effective urgency of a needs_human request."""
    top = str(raw.get("urgency") or "").strip().lower()
    if top:
        return top
    needs = raw.get("needs")
    if isinstance(needs, list):
        urgencies = [str(n.get("urgency") or "").strip().lower()
                     for n in needs if isinstance(n, dict)]
        urgencies = [u for u in urgencies if u]
        if any(u == "blocker" for u in urgencies):
            return "blocker"
        if urgencies:
            return urgencies[0]
    return "blocker"

def _extract_hitl_fallbacks(raw: dict) -> list[str]:
    """Pull documented fallback descriptions from a needs[]-shaped payload."""
    fallbacks: list[str] = []
    needs = raw.get("needs")
    if isinstance(needs, list):
        for n in needs:
            if not isinstance(n, dict):
                continue
            fb = str(n.get("fallback") or "").strip()
            if fb:
                key = str(n.get("key") or n.get("provider") or "").strip()
                fallbacks.append(f"{key}: {fb}" if key else fb)
    top_fb = str(raw.get("fallback") or "").strip()
    if top_fb:
        fallbacks.append(top_fb)
    return fallbacks

def _normalise_needs_human(raw: dict) -> dict:
    """Coerce a ``needs_human.json`` payload into a stable shape."""
    evidence = raw.get("evidence") or {}
    if isinstance(evidence, str):
        evidence = {"freeform": evidence}
    elif not isinstance(evidence, dict):
        evidence = {}
    if "tested_commands" not in evidence:
        if raw.get("commands_tried"):
            evidence["tested_commands"] = list(raw["commands_tried"])
        elif raw.get("tested_cmd"):
            evidence["tested_commands"] = [raw["tested_cmd"]]
    if "error_output" not in evidence and raw.get("error_output"):
        evidence["error_output"] = raw["error_output"]

    summary = str(raw.get("summary") or raw.get("reason") or "").strip()
    if not summary:
        needs = raw.get("needs")
        if isinstance(needs, list) and needs:
            parts = []
            for n in needs:
                if not isinstance(n, dict):
                    continue
                key = str(n.get("key") or n.get("provider") or "").strip()
                purpose = str(n.get("purpose") or "").strip()
                if key and purpose:
                    parts.append(f"{key}: {purpose}")
                elif key:
                    parts.append(key)
            if parts:
                summary = "; ".join(parts)[:500]

    return {
        "summary": summary,
        "stage": str(raw.get("stage") or raw.get("phase") or "").strip(),
        "what_failed": str(raw.get("what_failed") or raw.get("details") or "").strip(),
        "evidence": evidence,
        "options": _coerce_hitl_options(raw),
        "default_option": str(raw.get("default_option") or "").strip(),
        "timeout_minutes": int(raw.get("timeout_minutes") or 60),
        "urgency": _extract_hitl_urgency(raw),
        "fallbacks": _extract_hitl_fallbacks(raw),
    }

def _hitl_slug(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", s).strip("_").lower()
    return s[:max_len] or "anon"

def _append_hitl_history(code_dir: Path, req: dict, reply,
                         chosen, decision_text: str,
                         stage_label: str) -> Path:
    """Append a Q+A entry to ``results/needs_human_history.jsonl``."""
    history = code_dir / "results" / "needs_human_history.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(),
        "stage": stage_label,
        "request": req,
        "reply": reply,
        "chosen_option": chosen,
        "decision_text": decision_text,
    }
    with history.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return history

def _update_hitl_decisions(state_dir: Path, req: dict, chosen,
                            decision_text: str, stage_label: str) -> Path:
    """Write ``auto_research/state/hitl_decisions.yaml``."""
    path = state_dir / "hitl_decisions.yaml"
    data: dict = {}
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception:
            data = {}
    decisions = data.get("decisions", [])
    decision_id = _hitl_slug(f"{stage_label}::{req.get('summary','')}")
    record = {
        "id": decision_id,
        "timestamp": datetime.now().isoformat(),
        "stage": stage_label,
        "summary": req.get("summary"),
        "chosen_option": chosen,
        "free_text": None if chosen else (decision_text or None),
    }

    replaced = False
    for i, d in enumerate(decisions):
        if isinstance(d, dict) and d.get("id") == decision_id:
            decisions[i] = record
            replaced = True
            break
    if not replaced:
        decisions.append(record)

    data["decisions"] = decisions
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return path
