"""Nano Banana: Gemini-powered AI figure generation for scientific papers.

Uses Google's Gemini image generation models (Nano Banana Flash / Pro)
to create concept diagrams, architecture figures, and mechanism illustrations.

Supports two modes:
  - One-shot: generate_figure() — single image generation call (legacy)
  - Pipeline: generate_figure_pipeline() — Planner → Stylist → Visualizer → Critic loop

Models:
  - flash: gemini-3.1-flash-image  (fast, image generation)
  - pro:   gemini-3-pro-image       (highest quality, image generation)
"""

import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import yaml
from pathlib import Path
from typing import Optional

# Silence google-genai's repeated "Both GOOGLE_API_KEY and GEMINI_API_KEY are
# set" notice — ARK sets both env vars to the same key on purpose and passes the
# key explicitly to genai.Client. Drop only that message; keep other genai logs.
import logging as _logging
_logging.getLogger("google_genai._api_client").addFilter(
    lambda _r: "Both GOOGLE_API_KEY and GEMINI_API_KEY" not in _r.getMessage()
)


# ══════════════════════════════════════════════════════════════
#  Models
# ══════════════════════════════════════════════════════════════

IMAGE_MODELS = {
    "flash": "gemini-3.1-flash-image",
    "pro": "gemini-3-pro-image",
}

TEXT_MODELS = {
    "flash": "gemini-flash-latest",
    "pro": "gemini-pro-latest",
}

# Legacy alias
MODELS = IMAGE_MODELS
DEFAULT_MODEL = "flash"


# ══════════════════════════════════════════════════════════════
#  API Key
# ══════════════════════════════════════════════════════════════

def get_api_key() -> str:
    """Get Gemini API key (reuses deep_research key)."""
    from ark.deep_research import get_gemini_api_key
    return get_gemini_api_key()


# ══════════════════════════════════════════════════════════════
#  Style Guide Loader
# ══════════════════════════════════════════════════════════════

def _load_style_guide(guide_name: str) -> str:
    """Load a style guide from ark/templates/style_guides/.

    Args:
        guide_name: Name without extension (e.g. "academic_diagram_style")

    Returns:
        Style guide content string, or empty string if not found.
    """
    guide_path = Path(__file__).parent / "templates" / "style_guides" / f"{guide_name}.md"
    if guide_path.exists():
        return guide_path.read_text()
    return ""


# ══════════════════════════════════════════════════════════════
#  Gemini Text Helper
# ══════════════════════════════════════════════════════════════

def _call_gemini_text(client, model_id: str, prompt: str, image_bytes: bytes = None,
                      receipt: dict | None = None) -> str:
    """Call Gemini text model, optionally with an image for multimodal input.

    Args:
        client: google.genai.Client instance
        model_id: Text model ID (e.g. "gemini-flash-latest")
        prompt: Text prompt
        image_bytes: Optional PNG image bytes for multimodal (Critic) calls

    Returns:
        Response text string, or empty string on failure.
    """
    from google.genai import types

    try:
        if image_bytes:
            contents = [
                types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                prompt,
            ]
        else:
            contents = prompt

        response = client.models.generate_content(
            model=model_id,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["TEXT"],
                **({"media_resolution": "MEDIA_RESOLUTION_HIGH"} if image_bytes and receipt is not None else {}),
            ),
        )
        if receipt is not None:
            receipt.update(response_id=getattr(response, "response_id", None),
                           model_version=getattr(response, "model_version", None))
        return response.text or ""
    except Exception as e:
        print(f"Gemini text call error ({model_id}): {e}")
        return ""


# ══════════════════════════════════════════════════════════════
#  Image Generation (legacy, unchanged)
# ══════════════════════════════════════════════════════════════

def _save_generated_image(payload: bytes, output_path: Path, receipt: dict | None = None, **metadata) -> None:
    """Install decoded provider output atomically; preserve evidence across format conversion."""
    from PIL import Image

    output_path = Path(output_path)
    with Image.open(io.BytesIO(payload)) as image:
        image.load()
        target = Image.registered_extensions().get(output_path.suffix.lower())
        encoded = payload
        if target and image.format != target:
            buffer = io.BytesIO()
            image.save(buffer, format=target)
            encoded = buffer.getvalue()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=output_path.parent, prefix=f".{output_path.name}.")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
        os.replace(temp_name, output_path)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    if receipt is not None:
        receipt.update(metadata, payload_sha256=hashlib.sha256(payload).hexdigest(),
                       artifact_sha256=hashlib.sha256(encoded).hexdigest())


def generate_figure(
    prompt: str,
    output_path: Path,
    api_key: str = None,
    model: str = DEFAULT_MODEL,
    *, aspect_ratio: str | None = None, receipt: dict | None = None, reference_image: bytes | None = None,
) -> bool:
    """Generate a single figure using Nano Banana.

    Args:
        prompt: Detailed image generation prompt
        output_path: Where to save the PNG
        api_key: Gemini API key (auto-detected if None)
        model: "flash" or "pro"

    Returns:
        True if image was generated and saved successfully.
    """
    key = api_key or get_api_key()
    if not key:
        return False

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("Error: google-genai package not installed. Run: pip install google-genai")
        return False

    model_id = IMAGE_MODELS.get(model, model)

    try:
        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=model_id,
            contents=([types.Part.from_bytes(data=reference_image, mime_type="image/png"), prompt]
                      if reference_image else prompt),
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio=aspect_ratio) if aspect_ratio else None,
            ),
        )

        for part in response.parts:
            if part.inline_data and part.inline_data.data:
                _save_generated_image(part.inline_data.data, output_path, receipt,
                                      provider="google", image_model=model_id, payload_source="google_inline_data",
                                      model_version=getattr(response, "model_version", None),
                                      response_id=getattr(response, "response_id", None))
                return True

        return False
    except Exception as e:
        print(f"Nano Banana error: {e}")
        return False


SEMANTIC_REVIEW_VERSION = 3
_SEMANTIC_CHECKS = ("arrows", "comparison_groups", "numbers", "data_separation", "stages_and_labels", "printed_labels",
                    "result_marks")


def _verified_worker_excerpt(state_dir, receipt):
    relative = receipt.get("artifact", "")
    if (receipt.get("status") == "completed" and isinstance(relative, str)
            and re.fullmatch(r"results/[\w-]+\.json", relative)):
        result = Path(state_dir).parent.parent / relative
        if result.is_file() and result.resolve() == result and result.stat().st_size <= 16_000_000:
            data = result.read_bytes()
            if hashlib.sha256(data).hexdigest() == receipt.get("sha256"):
                parsed = json.loads(data)
                summary = parsed.get("summary", parsed) if isinstance(parsed, dict) else parsed
                return "Verified worker measurements (excerpt):\n" + json.dumps(summary)[:2600]


def native_study_design(state, state_dir):
    """Only the approved protocol goes to the illustration model."""
    plan = state.get("plan") or {}
    conditions = {k: plan[k] for k in ("question", "baseline", "comparison", "controls", "metric") if k in plan}
    return "Approved experimental design:\n" + yaml.safe_dump(conditions, sort_keys=False, allow_unicode=True)


def legacy_native_design_digest(state, state_dir):
    """Recognize an existing paid image made before the protocol and measurements were separated."""
    design = native_study_design(state, state_dir)
    plan = state.get("plan") or {}
    receipt = state.get("experiment") or {}
    plan_hash = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    if (receipt.get("plan_sha256") == plan_hash and receipt.get("artifact") == plan.get("expected")
            and (not state.get("sealed_script") or receipt.get("code_sha256") == state["sealed_script"])):
        if excerpt := _verified_worker_excerpt(state_dir, receipt):
            design += "\n" + excerpt
    return hashlib.sha256(design.encode()).hexdigest()


def concept_design(fig: dict, state_dir: Path | None) -> str:
    """The same bounded study design is used for inspection, editing and cache identity."""
    parts = [json.dumps(fig, sort_keys=True, ensure_ascii=False)]
    if state_dir:
        receipt_file = Path(state_dir) / "native_project.json"
        if fig.get("representation") in ("study_panel", "schematic") and receipt_file.is_file():
            design = native_study_design(json.loads(receipt_file.read_text()), state_dir)
            return design + (
                "\n\nProposed figure specification (presentation only). Check every illustrated condition and the "
                "proposed caption against the approved design above. Treat artwork that "
                "depicts unsupported classes, clusters or trends as a scientific contradiction. "
                "This request supplies no additional experimental facts.\n" + parts[0]
            )
        for name in ("idea.md", "project_context.md", "experiment_plan.yaml"):
            path = Path(state_dir) / name
            if not path.is_file():
                continue
            body = path.read_text(errors="replace")
            if name == "project_context.md":
                sections = re.split(r"(?=^## )", body, flags=re.M)
                body = "\n".join(section for section in sections if re.search(
                    r"(?i)experiment|research design|evaluation", section.split("\n")[0]))
            lines = [line for line in body.splitlines() if not re.search(
                r"(?i)api[_ -]?key|password|secret|credential|access[_ -]?token", line)]
            parts.append(name + "\n" + "\n".join(lines)[:2200])
    return "\n\n".join(parts)


def _openrouter_figure_request(key, model, prompt, pixels, *, aspect_ratio=None):
    """One owner-authorized vision request, optionally requesting an edited image."""
    import httpx
    content = [{"type": "text", "text": prompt}]
    if pixels:
        url = "data:image/png;base64," + base64.b64encode(pixels).decode()
        content.append({"type": "image_url", "image_url": {"url": url}})
    payload = {"model": model, "messages": [{"role": "user", "content": content}]}
    if aspect_ratio:
        payload.update(modalities=["image", "text"], image_config={"aspect_ratio": aspect_ratio})
    else:
        payload["max_tokens"] = 2000
    response = httpx.post("https://openrouter.ai/api/v1/chat/completions", json=payload,
                          headers={"Authorization": "Bearer " + key}, timeout=120)
    response.raise_for_status()
    return response.json()


_RESULT_TITLE = (r"(?:data derived|data driven|measured|observed|empirical|experimental) "
                 r"(?:(?:result|results|performance|measurement|comparison) )?(?:plots?|charts?|curves?|graphs?)")
_RESULT_AXIS = (r"(?:measured|observed|empirical|experimental|actual) "
                r"(?:results?|values?|outcomes?|responses?|measurements?|performance|errors?|accuracy|scores?)")
_TRIAL_AXIS = (r"(?:experimental |test |random |training |validation )?(?:seeds?|folds?|splits?|trials?|runs?|"
               r"samples?|epochs?|iterations?|conditions?|time|steps?)(?: index| id| number)?")


def _result_plot_labels(labels):
    """Require a graph claiming empirical provenance and separate labels describing its axes."""
    if not isinstance(labels, list):
        return None
    def match(pattern):
        return next((s for s in labels if isinstance(s, str) and
                     re.fullmatch(pattern, re.sub(r"[^\w]+", " ", s.casefold()).strip())), None)
    heading, response, axis = [match(pattern) for pattern in (_RESULT_TITLE, _RESULT_AXIS, _TRIAL_AXIS)]
    return (heading, response, axis) if heading and response and axis else None


def generated_result_conflict(checks):
    """Use the literal transcription or explicit observations of a plotted experimental outcome."""
    if not isinstance(checks, dict):
        return None
    marks = checks.get("result_marks")
    if (isinstance(marks, dict) and (marks.get("status") == "contradiction" or marks.get("depiction") == "experimental_results")
            and isinstance(marks.get("observed"), str) and marks["observed"].strip()):
        return marks["observed"]
    printed = checks.get("printed_labels")
    labels = _result_plot_labels(printed.get("transcription")) if isinstance(printed, dict) else None
    if labels:
        return "The generated illustration labels a results chart and its axes: " + ", ".join(repr(s) for s in labels) + "."
    return None


def reject_generated_result(review, reason):
    check = {"status": "contradiction", "observed": reason,
             "expected": "The method illustration describes the protocol and contains no empirical result marks.",
             "correction": "Remove the results chart from the illustration. Draw measured values separately from worker output."}
    return {**review, "status": "contradiction", "checks": {**review.get("checks", {}), "result_marks": check}}


def semantic_verdict(checks) -> str:
    """Derive a verdict from the actual check records, including literal transcription."""
    if generated_result_conflict(checks):
        return "contradiction"
    if not isinstance(checks, dict) or not all(k in checks for k in _SEMANTIC_CHECKS):
        return "unavailable"
    marks = checks.get("result_marks")
    if not isinstance(marks, dict) or marks.get("depiction") not in ("method", "unclear"):
        return "unavailable"
    printed = checks.get("printed_labels")
    transcription = printed.get("transcription") if isinstance(printed, dict) else None
    if not isinstance(transcription, list) or not transcription or not all(
            isinstance(label, str) and label.strip() for label in transcription):
        return "unavailable"
    states = []
    for k in _SEMANTIC_CHECKS:
        item = checks[k]
        if not isinstance(item, dict) or not isinstance(item.get("observed"), str) or not item["observed"].strip():
            return "unavailable"
        status = item.get("status")
        if status not in ("consistent", "contradiction", "unclear", "not_applicable"):
            return "unavailable"
        if status == "contradiction" and not all(isinstance(item.get(f), str) and item[f].strip()
                                                  for f in ("expected", "correction")):
            return "unavailable"
        states.append(status)
    verdict = ("contradiction" if "contradiction" in states else
               "unclear" if "unclear" in states or marks["depiction"] == "unclear" or "consistent" not in states else "passed")
    return verdict


def _one_letter_apart(a: str, b: str) -> bool:
    if min(len(a), len(b)) < 5 or not (a.isascii() and a.isalpha() and b.isascii() and b.isalpha()):
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long = sorted((a, b), key=len)
    return len(long) == len(short) + 1 and any(long[:i] + long[i+1:] == short for i in range(len(long)))


def _label_differences(labels, lines):
    """An entire multiword phrase must align uniquely with one differing English word."""
    differences = {}
    for label in labels:
        expected = label.split()
        normalized = [word.casefold() for word in expected]
        if len(expected) < 2 or not all(re.fullmatch("[a-z]+", word) for word in normalized):
            continue
        alternatives, exact = [], False
        for line in lines:
            for start in range(len(line) - len(expected) + 1):
                chunk = line[start:start + len(expected)]
                tokens = [row["text"].casefold() for row in chunk]
                diff = [i for i, (left, right) in enumerate(zip(normalized, tokens)) if left != right]
                if not diff:
                    exact = True
                    continue
                if len(diff) != 1 or any(row["confidence"] < 90 for row in chunk):
                    continue
                i = diff[0]
                if (expected[i].isupper() or chunk[i]["text"].isupper()
                        or not _one_letter_apart(normalized[i], tokens[i])):
                    continue
                alternatives.append({"model_label": label, "model_word": expected[i],
                                     "read_label": " ".join(r["text"] for r in chunk), **chunk[i]})
        if not exact and len(alternatives) == 1:
            differences[label] = alternatives[0]
    return differences


def _tesseract_lines(binary, path, mode):
    process = subprocess.run([binary, str(path), "stdout", "-l", "eng", "--psm", str(mode), "tsv"],
                             capture_output=True, check=True, timeout=25, text=True,
                             env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    lines = {}
    for row in csv.DictReader(io.StringIO(process.stdout), delimiter="\t", quoting=csv.QUOTE_NONE):
        if row.get("level") == "5" and row["text"].strip():
            group = tuple(row[k] for k in ("page_num", "block_num", "par_num", "line_num"))
            lines.setdefault(group, []).append({"text": row["text"].strip(), "confidence": float(row["conf"]),
                "box": [int(row[k]) for k in ("left", "top", "width", "height")]})
    return list(lines.values())


def _ocr_chart_labels(path, binary, runs):
    """Corroborate a chart title and its two axis labels, including vertical text missed by page OCR."""
    from PIL import Image

    def phrases(lines, pattern):
        found = []
        for line in lines:
            for i in range(len(line)):
                for size in range(min(5, len(line)-i), 0, -1):
                    words = line[i:i+size]
                    phrase = re.sub(r"[^\w]+", " ", " ".join(w["text"] for w in words).casefold()).strip()
                    if all(w["confidence"] >= 60 for w in words) and re.fullmatch(pattern, phrase):
                        boxes = [w["box"] for w in words]
                        found.append((phrase, (min(b[0] for b in boxes), min(b[1] for b in boxes),
                                               max(b[0]+b[2] for b in boxes), max(b[1]+b[3] for b in boxes))))
                        break
        return found

    def agreed(scans, pattern):
        first, second = [phrases(lines, pattern) for lines in scans]
        def overlaps(a, b):
            area = max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))
            union = (a[2]-a[0]) * (a[3]-a[1]) + (b[2]-b[0]) * (b[3]-b[1]) - area
            return area > 0 and area >= .35 * union
        return [(text, a) for text, a in first if any(text == other and overlaps(a,b) for other,b in second)]

    trials = agreed(runs, _TRIAL_AXIS)
    inspected = 0
    for heading, (x0, _, x1, top) in agreed(runs, _RESULT_TITLE):
        for axis, (a0, bottom, a1, _) in trials:
            width = max(x1-x0, a1-a0)
            if bottom <= top+15 or abs((x0+x1-a0-a1)/2) > width/2:
                continue
            if inspected == 3:
                return []
            inspected += 1
            # A vertical response axis sits beside the plot, between its heading and horizontal axis.
            with Image.open(path) as image, tempfile.TemporaryDirectory() as folder:
                bounds = (max(0, int(min(x0,a0)-width)), top, min(image.width, int(min(x0,a0)+width*.25)), bottom)
                if bounds[2] <= bounds[0]:
                    continue
                strip = image.crop(bounds)
                rotated = Path(folder) / "axis.png"
                strip.transpose(Image.Transpose.ROTATE_270).save(rotated)
                vertical = agreed([_tesseract_lines(binary, rotated, mode) for mode in (6, 11)], _RESULT_AXIS)
                if vertical:
                    return [heading, vertical[0][0], axis]
    return []


def verify_printed_labels(path: Path, review: dict, entry: dict, aspect_ratio: str) -> dict:
    """Augment a saved visual review without another model request."""
    checks = review.get("checks") or {}
    if reason := generated_result_conflict(checks):
        return reject_generated_result(review, reason)
    labels = (checks.get("printed_labels") or {}).get("transcription")
    if not isinstance(labels, list) or not all(isinstance(word, str) for word in labels):
        return review
    pixel_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    label_hash = hashlib.sha256(json.dumps(labels, ensure_ascii=False).encode()).hexdigest()
    prior = review.get("label_verification") or {}
    binary = shutil.which(os.environ.get("ARK_TESSERACT") or "tesseract")
    is_panel = entry.get("representation") == "subject_with_comparison_panel"
    if (prior.get("version") == 2 and prior.get("image_sha256") == pixel_hash
            and prior.get("transcription_sha256") == label_hash and (not binary or prior.get("chart_ocr")) and
            (prior.get("coverage") in ("two_ocr_layouts", "reproduced_typeset")
             or (not is_panel and not binary and prior.get("coverage") == "unavailable"))):
        return review
    evidence = {"version": 2, "image_sha256": pixel_hash, "transcription_sha256": label_hash,
                "coverage": "unavailable", "model_label_count": len(labels), "conflicts": []}
    if is_panel:
        raw = (path.parent / (entry.get("illustration_file") or "")).resolve()
        groups = (entry.get("panel_plan") or {}).get("groups") or []
        try:
            if (raw.is_relative_to((path.parent / ".concept_sources").resolve()) and raw.is_file()
                    and hashlib.sha256(raw.read_bytes()).hexdigest() == entry.get("payload_sha256")):
                with tempfile.TemporaryDirectory() as folder:
                    reproduced = Path(folder) / "panel.png"
                    _compose_study_panel(raw, groups, reproduced, single_column=aspect_ratio == "4:3")
                    if hashlib.sha256(reproduced.read_bytes()).hexdigest() == pixel_hash:
                        printed = [g[k] for g in groups for k in ("label", "description")]
                        lines = [[{"text": w, "confidence": 100, "box": []} for w in line.split()] for line in printed]
                        evidence.update(coverage="reproduced_typeset", labels=printed,
                                        conflicts=list(_label_differences(labels, lines).values()))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if binary:
        try:
            runs = [_tesseract_lines(binary, path, mode) for mode in (6, 11)]
            if all(runs):
                evidence["chart_ocr"] = True
                if evidence["coverage"] == "unavailable":
                    first, second = [_label_differences(labels, lines) for lines in runs]
                    conflicts = []
                    for label, a in first.items():
                        b = second.get(label)
                        if (b and a["read_label"].casefold() == b["read_label"].casefold()
                                and max(abs(x-y) for x, y in zip(a["box"], b["box"])) <= 3):
                            conflicts.append({**a, "confirming_confidence": b["confidence"], "confirming_box": b["box"]})
                    unique = [c for c in conflicts if sum(
                        x["box"] == c["box"] and x["text"] == c["text"] for x in conflicts) == 1]
                    evidence.update(coverage="two_ocr_layouts", psm=[6, 11], conflicts=unique)
                observed = [_result_plot_labels([" ".join(w["text"] for w in line) for line in lines
                                                 if all(w["confidence"] >= 90 for w in line)]) for lines in runs]
                if observed[0] and observed[0] == observed[1]:
                    evidence["result_plot_labels"] = observed[0]
                elif markers := _ocr_chart_labels(path, binary, runs):
                    evidence["result_plot_labels"] = markers
        except (OSError, subprocess.SubprocessError, KeyError, ValueError, TypeError):
            pass
    result = {**review, "label_verification": evidence}
    if evidence.get("result_plot_labels"):
        return reject_generated_result(result, "Two OCR layouts read the empirical chart title and its axes: "
                                       + ", ".join(repr(s) for s in evidence["result_plot_labels"]) + ".")
    if evidence["conflicts"]:
        heard = ", ".join(f"{v['read_label']!r} vs {v['model_label']!r}" for v in evidence["conflicts"])
        printed = {**checks["printed_labels"], "status": "contradiction",
                   "observed": "Printed phrase and visual transcription disagree: " + heard,
                   "expected": "The printed label must literally match the intended study label.",
                   "correction": "Correct the printed phrase at the identified location: " + heard}
        result.update(checks={**checks, "printed_labels": printed}, status="contradiction")
    return result


def review_concept_image(path: Path, design: str, provider: str, key: str, model: str) -> dict:
    """Assess the actual selected pixels. Only concrete contradictions authorize an edit."""
    pixels = path.read_bytes()
    result = {"status": "unavailable", "provider": provider, "model": model}
    prompt = f"""Scientifically audit this final manuscript image against the study design below.
Inspect the attached pixels independently. Trace each arrow stem to its visible arrowhead:
identify both endpoint labels, the input/output, and which branch receives the value. Check shared
inputs versus treatment-only fitted parameters, baseline/control isolation, group identities,
sample counts, numeric labels and units, held-out data and leakage. Read every visible stage label:
look for wrong/garbled scientific labels, duplicated splits/stages or counts with conflicting
roles, and unconnected boxes presenting a spurious operation. A repeated label identifying a
shared dataset in separate legitimate stages is acceptable. For printed_labels, transcribe every printed
heading and method label literally, including repeated or garbled words. Read letter by letter; never
silently correct a misspelling to the intended word. Supply a nonempty "transcription" array of those
literal strings (use ["no printed labels"] only for an entirely unlabeled picture). Misspelled ordinary
words and corrupted scientific names are contradictions even if the underlying science is correct.
If a word cannot be deciphered mark unclear, not consistent. Ignore purely aesthetic preferences.
For result_marks inspect every panel, including insets. Identify any plotted marks, their axis labels
and legend. Set depiction to experimental_results if dots, bars or curves claim to display observed
study outcomes, and mark contradiction: generative artwork cannot publish measured results. A schematic
neighborhood, conceptual distribution or operation such as "compute RMSE" may be consistent.
For these use depiction method; use unclear if the distinction cannot be read.
Report a contradiction only when pixels and design establish different meanings. If an endpoint
or label cannot be read, mark unclear. A check outside this design can be not_applicable.
Return only JSON with a checks object containing exactly {", ".join(_SEMANTIC_CHECKS)}.
Each check must have status consistent, contradiction, unclear or not_applicable, and an observed
string describing what is actually visible. For each contradiction also include expected (specific
design fact) and correction (one precise edit). Do not let a correct caption override wrong pixels.
Study design and contextual evidence:
{design}"""
    try:
        if provider == "openrouter":
            response = _openrouter_figure_request(key, model, prompt, pixels)
            text = response["choices"][0]["message"]["content"]
            result.update(response_id=response.get("id"), model_version=response.get("model"))
        else:
            from google import genai
            text = _call_gemini_text(genai.Client(api_key=key), model, prompt, pixels, receipt=result)
        from ark.quality_gate import extract_json
        data = extract_json(text, want=dict) if text else None
        checks = data.get("checks") if isinstance(data, dict) else None
        if reason := generated_result_conflict(checks):
            return reject_generated_result({**result, "checks": {k: v for k, v in checks.items() if k in _SEMANTIC_CHECKS}}, reason)
        verdict = semantic_verdict(checks)
        if verdict == "unavailable":
            return result
        result.update(status=verdict, checks={k: checks[k] for k in _SEMANTIC_CHECKS})
    except Exception:
        # Never store an HTTP exception body or owner credential in the manifest.
        pass
    return result


def repair_concept_image(path: Path, design: str, review: dict, provider: str, key: str,
                         model: str, aspect_ratio: str, receipt: dict) -> bool:
    changes = [v["correction"] for v in review["checks"].values() if v["status"] == "contradiction"]
    prompt = ("Edit the attached scientific diagram. Keep its legible, accurate labels, geometry and "
              "scientific relationships. Preserve every required comparison arm, endpoint and shared input. If a line is wrong, reroute its arrowhead without deleting either node or merging the groups. Correct every documented contradiction. Simplify clutter when "
              "necessary to make arrow endpoints unambiguous. Return only the complete corrected diagram."
              "\nRequired changes:\n- " + "\n- ".join(changes) + "\nStudy design:\n" + design)
    pixels = path.read_bytes()
    if provider == "google":
        return generate_figure(prompt, path, key, model, aspect_ratio=aspect_ratio,
                               receipt=receipt, reference_image=pixels)
    try:
        response = _openrouter_figure_request(key, model, prompt, pixels, aspect_ratio=aspect_ratio)
        url = response["choices"][0]["message"]["images"][0]["image_url"]["url"]
        if not url.startswith("data:image/") or ";base64," not in url:
            return False
        _save_generated_image(base64.b64decode(url.split(",", 1)[1], validate=True), path, receipt,
                              provider=provider, image_model=model, model_version=response.get("model"),
                              response_id=response.get("id"), payload_source="openrouter_inline_data")
        return True
    except Exception:
        return False


def _compose_study_panel(illustration: Path, groups: list, destination: Path, single_column: bool):
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    width = 840 if single_column else 1600
    margin, padding = 28, 24
    card_x = margin if single_column else 620
    card_width = width - margin - card_x
    heading = ImageFont.truetype("DejaVuSans-Bold.ttf", 32 if single_column else 43)
    body = ImageFont.truetype("DejaVuSans.ttf", 29 if single_column else 36)
    def wrap(text, font):
        lines = [""]
        for word in text.split():
            line = (lines[-1] + " " + word).strip()
            if font.getlength(line) <= card_width - 2 * padding:
                lines[-1] = line
            elif not lines[-1] or font.getlength(word) > card_width - 2 * padding:
                raise ValueError("Panel label cannot fit at print size")
            else:
                lines.append(word)
        return lines
    sections = [(wrap(g["label"], heading), wrap(g["description"], body)) for g in groups]
    heights = [2 * padding + len(names) * (heading.size + 9) + len(lines) * (body.size + 8)
               for names, lines in sections]
    top = 245 if single_column else margin
    height = max(top + sum(heights) + 14 * (len(groups) - 1) + margin, 560)
    canvas = Image.new("RGB", (width, height), "white")
    art_box = (width - 2 * margin, top - 2 * margin) if single_column else (card_x - 2 * margin, height - 2 * margin)
    with Image.open(illustration) as im:
        art = ImageOps.contain(im.convert("RGB"), art_box, Image.Resampling.LANCZOS)
        canvas.paste(art, (margin + (art_box[0] - art.width) // 2, margin + (art_box[1] - art.height) // 2))
    draw = ImageDraw.Draw(canvas)
    for i, ((names, lines), h) in enumerate(zip(sections, heights)):
        draw.rounded_rectangle((card_x, top, width - margin, top + h), radius=14,
                               fill=("#edf4f7", "#f0f4ec", "#f7f1ec")[i], outline="#68767c", width=2)
        y = top + padding - 3
        for text, font in [(t, heading) for t in names] + [(t, body) for t in lines]:
            draw.text((card_x + padding, y), text, font=font, fill="#172428")
            y += font.size + (9 if font is heading else 8)
        top += h + 14
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    _save_generated_image(buffer.getvalue(), destination)


def _study_panel_plan(design, provider, key, model):
    """Recover the comparison when a legacy project only stores prose."""
    from ark.quality_gate import extract_json
    prompt = ("The source describes a scientific comparison. Return JSON with subject (one grounded "
              "physical or conceptual study object to illustrate, without a protocol), groups "
              "(2 or 3 distinct required comparisons or study conditions, each with label up to 40 "
              "characters, description up to 125 characters, and evidence copied VERBATIM from the "
              "provided design), and caption (up to 500 characters explaining the shared data, "
              "conditions, control and assessment). Cover every required comparison. Do not create "
              "a panel if the groups cannot be supported. Write ordinary readable text without LaTeX."
              "\nStudy design:\n" + design)
    if provider == "google":
        from google import genai
        response = _call_gemini_text(genai.Client(api_key=key), model, prompt)
    else:
        response = _openrouter_figure_request(key, model, prompt, None)["choices"][0]["message"]["content"]
    plan = extract_json(response, want=dict)
    if not plan or not isinstance(plan.get("subject"), str) or not 2 <= len(plan["subject"]) <= 400:
        return None
    groups = plan.get("groups")
    if (not isinstance(groups, list) or not 2 <= len(groups) <= 3
            or not isinstance(plan.get("caption"), str) or not 2 <= len(plan["caption"]) <= 500):
        return None
    for g in groups:
        if (not isinstance(g, dict) or not all(isinstance(g.get(k), str) and g[k].strip()
                for k in ("label", "description", "evidence")) or len(g["label"]) > 40
                or len(g["description"]) > 125 or g["evidence"] not in design):
            return None
    if len({g["label"].casefold() for g in groups}) != len(groups):
        return None
    return plan


def create_study_panel(path: Path, design: str, provider: str, key: str, model: str, aspect_ratio: str,
                       *, approved_plan: dict | None = None):
    """Typeset the approved arms and generate only the subject illustration."""
    try:
        if approved_plan is None:
            plan = _study_panel_plan(design, provider, key, model)
            if not plan:
                return None
        else:
            baseline, comparison = approved_plan["baseline"], approved_plan["comparison"]
            if not baseline.strip() or not comparison.strip():
                return None
            plan = {
                "subject": approved_plan["question"],
                "groups": [{"label": label, "description": arm, "evidence": arm}
                           for label, arm in (("Baseline", baseline), ("Comparison", comparison))],
                "caption": (f"The study compares {baseline.rstrip('.')} with {comparison.rstrip('.')}. "
                            f"The protocol evaluates {approved_plan['metric'].rstrip('.')}. "
                            "The artwork illustrates the study subject."),
            }
        groups = plan["groups"]
        art_prompt = ("Create one clean, publication-quality scientific illustration of this single "
                      "study object, centered on white: " + plan["subject"] + ". No words, numerals, "
                      "letters, arrows, branches, workflows, charts, comparisons or layout panels. "
                      "It illustrates the subject only. For abstract inputs, depict their structure neutrally. "
                      "Do not invent classes, clusters or trends or present simulated data as an observation.")
        flash = IMAGE_MODELS["flash"]
        original = path.parent / ".concept_sources" / (path.stem + "-" + hashlib.sha256(design.encode()).hexdigest()[:12] + ".image")
        receipt = {}
        if provider == "google":
            if not generate_figure(art_prompt, original, key, flash, aspect_ratio="1:1", receipt=receipt):
                return None
        else:
            flash = "google/" + flash
            res = _openrouter_figure_request(key, flash, art_prompt, None, aspect_ratio="1:1")
            url = res["choices"][0]["message"]["images"][0]["image_url"]["url"]
            if not url.startswith("data:image/") or ";base64," not in url:
                return None
            _save_generated_image(base64.b64decode(url.split(",", 1)[1], validate=True), original, receipt,
                                  provider=provider, image_model=flash, response_id=res.get("id"),
                                  model_version=res.get("model"), payload_source="openrouter_inline_data")
        _compose_study_panel(original, groups, path, single_column=aspect_ratio == "4:3")
        receipt.update(artifact_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                       selected_candidate="typeset_study_panel", representation="subject_with_comparison_panel",
                       illustration_file=str(original.relative_to(path.parent)), panel_plan=plan)
        return receipt
    except Exception:
        return None


def generate_concept_figures(
    figures: list,
    figures_dir: Path,
    api_key: str = None,
    model: str = DEFAULT_MODEL,
    log_fn=None,
) -> list:
    """Batch generate concept figures from a list of descriptions.

    Args:
        figures: List of dicts with keys: name (str), prompt (str)
        figures_dir: Directory to save generated PNGs
        api_key: Gemini API key
        model: "flash" or "pro"
        log_fn: Optional logging function

    Returns:
        List of successfully generated file paths.
    """
    key = api_key or get_api_key()
    if not key:
        if log_fn:
            log_fn("No Gemini API key found, skipping AI figure generation", "WARN")
        return []

    generated = []
    figures_dir = Path(figures_dir)
    figures_dir.mkdir(parents=True, exist_ok=True)

    for fig in figures:
        name = fig.get("name", "figure")
        prompt = fig.get("prompt", "")
        if not prompt:
            continue

        output_path = figures_dir / f"{name}.png"
        if log_fn:
            log_fn(f"Generating figure: {name}...", "INFO")

        ok = generate_figure(prompt, output_path, api_key=key, model=model)
        if ok:
            if log_fn:
                log_fn(f"  Generated: {output_path.name}", "INFO")
            generated.append(str(output_path))
        else:
            if log_fn:
                log_fn(f"  Failed to generate: {name}", "WARN")

    return generated


# ══════════════════════════════════════════════════════════════
#  Prompt Generation (legacy, unchanged)
# ══════════════════════════════════════════════════════════════

def build_figure_prompt(
    figure_name: str,
    caption: str,
    paper_context: str,
    venue: str = "",
    column_width_in: float = 3.333,
) -> str:
    """Build a detailed image generation prompt for a scientific figure.

    Uses the paper context and caption to create a prompt that produces
    a clean, publication-ready concept diagram.
    """
    return f"""Create a professional scientific figure for an academic paper.

Figure: {figure_name}
Caption: {caption}

Context from the paper:
{paper_context[:2000]}

Style requirements:
- Clean white background, suitable for {venue or 'academic'} conference
- Professional scientific illustration style
- Clear, readable labels and annotations (minimum 10pt equivalent)
- No decorative elements, focus on clarity and accuracy
- Proportions suitable for {column_width_in:.1f} inch column width
- Use a consistent color palette (blues, greens, oranges for different components)
- Vector-like clean edges and shapes
- Include a clear legend if multiple elements are present

Generate a high-quality scientific concept diagram that would be suitable
for a top-tier academic publication."""


# ══════════════════════════════════════════════════════════════
#  Pipeline: Planner → Stylist → Visualizer → Critic
# ══════════════════════════════════════════════════════════════

def _run_planner(client, text_model_id: str, figure_name: str, caption: str,
                 paper_context: str, venue: str, column_width_in: float) -> str:
    """Plan a detailed visual description for a concept diagram.

    Returns a comprehensive textual specification of what the figure should contain,
    including layout, colors, shapes, arrows, and text labels.
    """
    prompt = f"""You are a world-class scientific illustration designer. Your task is to create a detailed visual specification for a concept diagram that will be rendered by an AI image generation model. The output should look like a **modern tech illustration** (think Figma or professional design tool output), NOT a basic flowchart or matplotlib diagram.

## Figure Information
- Figure name: {figure_name}
- Caption: {caption}
- Target venue: {venue or 'top-tier academic conference (NeurIPS/ICML level)'}
- Target width: {column_width_in:.1f} inches, landscape aspect ratio (~16:9)

## Paper Context
{paper_context[:3000]}

## Your Task

Design a PUBLICATION-QUALITY illustration. For each element, be extremely specific:

### 1. Composition & Layout
- Flow direction (left-to-right preferred for pipelines, top-to-bottom for hierarchies)
- How to group related components into background zones
- Visual hierarchy: what's the MAIN path the reader's eye should follow?
- Where to place secondary/error/feedback paths (de-emphasized)

### 2. Components (for EACH module/component)
- Shape: rounded rectangle (12px radius), cylinder (for storage only), diamond (for decisions)
- Fill color: assign HEX codes based on semantic role:
  * Input/data source → #90CAF9 (blue) on #E6F3FF zone
  * Processing/transformation → #80CBC4 (teal) on #E0F2F1 zone
  * Decision/classification → #FFB74D (orange) on #FFF3E0 zone
  * Output/result → #CE93D8 (purple) on #F3E5F5 zone
  * Error/rejection → #EF9A9A (red) on #FFEBEE zone
  * Storage/memory → #B0BEC5 (grey) on #ECEFF1 zone
- Border: 1px solid, slightly darker than fill (e.g., fill #90CAF9 → border #42A5F5)
- Text label: bold sans-serif, 12-14pt
- Icon suggestion: small relevant icon inside (shield for security, gear for processing, magnifying glass for detection, lock for access control, brain for AI, etc.)

### 3. Connections
- Main forward flow: solid dark grey (#424242) lines, 1.5px, orthogonal (right-angle) routing
- Secondary/auxiliary paths: dashed lines, lighter grey (#9E9E9E), 1px
- Error/rejection paths: dashed red (#EF5350) lines, 1px
- Arrow labels: regular sans-serif, 9pt, placed along the line
- Arrowheads: filled, proportional to line width

### 4. Background Zones
- Soft pastel backgrounds at 10-15% opacity to group related stages
- Generous padding (20px+) between components and zone edges
- Zone labels: italic sans-serif, 8-9pt, top-left corner of zone
- Zones should have rounded corners and soft edges

### 5. Visual Richness & Density
- The diagram should feel RICH and INFORMATIVE — not empty or sparse
- Add small illustrative icons/thumbnails inside key components (2D vector-style, NOT emoji)
- Use different shapes for different component types (not all rounded rectangles)
- Overall background: white or off-white (#FAFAFA)
- NO drop shadows, NO gradients, NO 3D effects. Flat design with semantic richness.
- Every visual element should encode meaning — if a color/shape/line doesn't convey information, remove it

### 6. TEXT vs VISUAL BALANCE (CRITICAL)
**Text must be SHORT, but visuals must be RICH.** These are different things.

TEXT rules (keep it minimal):
- Component labels: MAX 3-5 words. NO sentences inside components.
- Connection labels: MAX 1-3 words (e.g., "Benign", "Repaired")
- NO paragraphs, bullet lists, or multi-line text inside components
- Total visible text in the figure: under ~60 words

VISUAL rules (make it rich and detailed):
- Each component should have a DETAILED, recognizable icon (not a simple flat shape — e.g., a magnifying glass hovering over a document for detection, gears with a wrench for repair, a shield with embedded lock for security, a brick wall with a scanning beam for firewall)
- Show internal sub-structure visually: nested mini-elements, small thumbnails, overlapping shapes
- Use visual metaphors: a funnel for filtering, a pipeline for flow, stacked layers for hierarchy

LAYOUT rules (keep it compact):
- MINIMIZE whitespace between components. Pack elements closely.
- Components should feel tightly arranged with clear, short connections
- Avoid large empty areas — if there's space, add a visual annotation or detail
- The diagram should feel DENSE and INFORMATIVE even without reading the text

IMPORTANT: Do NOT include font sizes (e.g., "12pt"), hex color codes (e.g., "#E6F3FF"), or CSS-like properties in component descriptions. Those are for the style guide only. Just describe WHAT to draw — shapes, labels, connections, zones, icons — in plain language. The image generator will interpret font specs as literal text to render.

Output ONLY the visual specification. No preamble."""

    return _call_gemini_text(client, text_model_id, prompt)


def _run_stylist(client, text_model_id: str, description: str, style_guide: str) -> str:
    """Refine a visual description to match academic publication aesthetics.

    Preserves all semantic content while improving visual styling.
    """
    prompt = f"""You are a senior art director for top-tier AI conferences (NeurIPS, ICML, ICLR). Your task is to elevate a figure description from "functional diagram" to "publication-quality illustration."

## RULES
- PRESERVE all semantic content, logic, structure, and components
- ELEVATE the visual quality — make it look like professional design, not a basic flowchart
- ENFORCE the style guide strictly (HEX colors, typography hierarchy, icon suggestions)

## Style Guide
{style_guide}

## Figure Description to Refine
{description}

## Your Refinement Checklist

1. **Colors**: Verify every component has a HEX code matching its semantic role (input→blue, processing→green, etc.). Replace vague colors ("light blue") with exact HEX.

2. **Typography hierarchy**: Ensure exactly 3 levels:
   - Component names: bold sans-serif 12-14pt
   - Connection labels: regular sans-serif 9-10pt
   - Zone labels / annotations: light/italic 8-9pt

3. **Visual depth**: Add these if missing:
   - Subtle shadows (2px, 8% opacity) under main components
   - 12px rounded corners on all rectangular elements
   - Thin border strokes (1px, 15-20% darker than fill)
   - Soft edges on background zones

4. **Icons**: Suggest a small icon for each major component (shield, gear, magnifying glass, lock, brain, document, etc.)

5. **Composition**: Ensure the main flow path is visually dominant. Secondary paths (error, feedback) should be thinner, lighter, dashed.

6. **Anti-patterns**: Remove any flowchart-like flat styling. The output must feel like a Figma illustration, not PowerPoint.

Output ONLY the refined description. No preamble."""

    return _call_gemini_text(client, text_model_id, prompt)


def _run_critic(client, text_model_id: str, description: str, image_bytes: bytes,
                caption: str, style_guide: str) -> dict:
    """Evaluate a generated figure against its description and style guide.

    Returns a dict with scores and suggestions. Uses multimodal input
    (image + text) to visually assess the generated figure.
    """
    prompt = f"""You are an elite visual design critic for NeurIPS/ICML papers. You have extremely high standards. A score of 5 means "this could appear in a best paper award submission." You almost never give 5.

Examine the attached figure critically.

## Figure Caption
{caption}

## Visual Description (what was requested)
{description[:3000]}

## Style Guide Reference
{style_guide[:2000]}

## AUTOMATIC FAILURES (check FIRST — any match caps the score)

- Figure title/heading inside image → Aesthetics ≤ 3
- Text overlap or occlusion → Readability ≤ 2
- Missing described components → Faithfulness ≤ 3
- Looks like a basic flowchart (generic boxes + arrows, no visual richness) → Publication Readiness ≤ 2
- Only 1-2 colors / monotone → Publication Readiness ≤ 2
- Heavy black outlines or PowerPoint defaults → Publication Readiness ≤ 2
- No semantic color coding (colors are random, don't encode meaning) → Publication Readiness ≤ 3
- All text same size (no typography hierarchy) → Publication Readiness ≤ 3
- Sparse/empty layout with too much white space → Publication Readiness ≤ 3
- Drop shadows or 3D effects (should be FLAT design) → Aesthetics ≤ 3
- Text overload: sentences, paragraphs, or bullet lists inside components → Conciseness ≤ 2
- Component labels longer than 5 words → Conciseness ≤ 3
- Total visible text exceeds ~50 words → Conciseness ≤ 3

## Scoring Criteria (1-5, be VERY strict)

1. **Faithfulness** (1-5): All described components present? Connections correct? Labels accurate?

2. **Conciseness** (1-5): No visual clutter? No unnecessary text? Every element serves a purpose?

3. **Readability** (1-5): Clear flow? Labels readable at print size? No overlap? Good spacing?

4. **Aesthetics** (1-5): Professional color palette? Semantic color coding? Consistent styling?

5. **Publication Readiness** (1-5): THE MOST IMPORTANT CRITERION.
   - Does this look like a figure from a NeurIPS/ICML best paper?
   - Is it RICH and INFORMATIVE — densely packed with semantic elements but still clear?
   - Does it use semantic color coding (colors encode meaning, not decoration)?
   - Does it use different shapes for different component types?
   - Does it have typography hierarchy (bold for components, regular for labels, italic for math)?
   - Does it have small icons/illustrations inside components to reinforce meaning?
   - Is it flat design (NO shadows, NO 3D, NO gradients)?
   - 1 = looks like matplotlib/PowerPoint. 3 = clean but generic. 5 = NeurIPS best paper quality.

## Output (STRICT JSON, nothing else)

```json
{{
  "faithfulness": <1-5>,
  "conciseness": <1-5>,
  "readability": <1-5>,
  "aesthetics": <1-5>,
  "publication_readiness": <1-5>,
  "overall": <1-5>,
  "critic_suggestions": "<specific improvements, or 'No changes needed.'>",
  "revised_description": "<COMPLETE revised visual description if changes needed, or empty string>"
}}
```

IMPORTANT: If publication_readiness ≤ 3, you MUST provide a revised_description that adds specific visual polish instructions (shadows, icons, typography hierarchy, semantic colors)."""

    text = _call_gemini_text(client, text_model_id, prompt, image_bytes=image_bytes)
    if not text:
        return {"faithfulness": 3, "conciseness": 3, "readability": 3, "aesthetics": 3,
                "overall": 3, "critic_suggestions": "", "revised_description": ""}

    # Parse JSON from response (may have markdown fences)
    try:
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            result = json.loads(json_match.group())
            # Ensure required keys
            for key in ("faithfulness", "conciseness", "readability", "aesthetics",
                        "publication_readiness", "overall", "critic_suggestions", "revised_description"):
                if key not in result:
                    result[key] = 3 if key not in ("critic_suggestions", "revised_description") else ""
            return result
    except (json.JSONDecodeError, AttributeError):
        pass

    return {"faithfulness": 3, "conciseness": 3, "readability": 3, "aesthetics": 3,
            "overall": 3, "critic_suggestions": "", "revised_description": ""}


# ══════════════════════════════════════════════════════════════
#  Pipeline Entry Point
# ══════════════════════════════════════════════════════════════

def generate_figure_pipeline(
    figure_name: str,
    caption: str,
    paper_context: str,
    output_path: Path,
    api_key: str = None,
    model: str = DEFAULT_MODEL,
    venue: str = "",
    column_width_in: float = 3.333,
    max_critic_rounds: int = 3,
    log_fn=None,
    *, aspect_ratio: str = "16:9", receipt: dict | None = None,
) -> bool:
    """Generate a concept figure using the full Planner → Stylist → Critic pipeline.

    This is the recommended entry point for high-quality figure generation.
    Uses Gemini text models for planning/styling/critiquing and Gemini image
    models for actual generation.

    Args:
        figure_name: Short name for the figure (e.g. "fig_overview")
        caption: Figure caption from LaTeX
        paper_context: Relevant text from the paper (method section, etc.)
        output_path: Where to save the final PNG
        api_key: Gemini API key (auto-detected if None)
        model: "flash" or "pro" (controls both text and image model tier)
        venue: Target venue name (e.g. "NeurIPS", "ICML")
        column_width_in: Target figure width in inches
        max_critic_rounds: Maximum Critic feedback loops (default 3)
        log_fn: Optional logging function(msg, level)

    Returns:
        True if figure was generated successfully.
    """
    def _log(msg, level="INFO"):
        if log_fn:
            log_fn(msg, level)
        else:
            print(f"[{level}] {msg}")

    key = api_key or get_api_key()
    if not key:
        _log("No Gemini API key found", "WARN")
        return False

    try:
        from google import genai
    except ImportError:
        _log("google-genai package not installed. Run: pip install google-genai", "ERROR")
        return False

    client = genai.Client(api_key=key)
    text_model = TEXT_MODELS.get(model, TEXT_MODELS[DEFAULT_MODEL])
    output_path = Path(output_path)

    # Load style guide
    style_guide = _load_style_guide("academic_diagram_style")

    # ── Step 1: Planner ──
    _log(f"[Planner] Creating visual spec for {figure_name}...")
    description = _run_planner(client, text_model, figure_name, caption,
                               paper_context, venue, column_width_in)
    if not description:
        _log("Planner failed to generate description, falling back to one-shot", "WARN")
        prompt = build_figure_prompt(figure_name, caption, paper_context, venue, column_width_in)
        return generate_figure(prompt, output_path, api_key=key, model=model,
                               aspect_ratio=aspect_ratio, receipt=receipt)

    # ── Step 2: Stylist ──
    _log(f"[Stylist] Refining visual spec with style guide...")
    styled_description = _run_stylist(client, text_model, description, style_guide)
    if not styled_description:
        _log("Stylist failed, using Planner output directly", "WARN")
        styled_description = description

    # ── Step 3: Visualizer → Critic Loop ──
    generated = False
    for round_idx in range(max(1, max_critic_rounds)):
        # Build image generation prompt
        image_prompt = f"""Generate a publication-quality scientific methodology diagram for a top-tier AI conference paper (NeurIPS/ICML level).

{styled_description}

MANDATORY VISUAL STYLE — "Soft Tech & Scientific Pastels":
- Aesthetic: approachable yet precise. Think NeurIPS 2024-2025 best paper figures.
- Background zones: use VERY LIGHT desaturated pastels (10-15% opacity) — cream #F5F5DC, pale blue #E6F3FF, mint #E0F2F1, lavender #F3E5F5 — to group related components. NOT solid colored boxes.
- Component shapes: rounded rectangles (5-10px radius) for processes. Use different shapes for different types: cylinders for databases, diamonds for decisions, parallelograms for I/O.
- Fill colors: medium saturation pastels. Warm tones (salmon, peach, coral) for active/trainable components. Cool tones (sky blue, mint, lavender) for static/fixed components.
- Lines: orthogonal (right-angle) routing for architecture flows. Curved Bezier for feedback loops. Solid dark grey for forward flow, dashed lighter grey for auxiliary paths, dashed red for error paths.
- Typography: sans-serif BOLD for component names, sans-serif regular for connection labels, serif italic for any math notation. Clear size hierarchy.
- Small illustrative icons or thumbnails inside components to reinforce meaning — these should be simple, clean 2D vector-style icons (NOT emoji).
- The diagram should feel RICH and INFORMATIVE — densely packed with semantic elements but still organized and clear. Avoid empty space.
- Every visual element (color, shape, line style) should encode meaning, not just decoration.

ABSOLUTE PROHIBITIONS:
- NO figure title or heading text at the top of the image
- NO subtitle or description text — ONLY component labels and connection labels
- NO font specifications rendered as text (do NOT render "9pt", "Bold", "Sans-Serif", hex codes like "#424242" as visible text in the image)
- NO heavy black outlines or borders. Use thin, subtle strokes.
- NO 3D effects, gradients, or textures (unless encoding data dimensionality)
- NO drop shadows (flat design, not material design)
- Proportions: landscape, aspect ratio {aspect_ratio}, width ~{column_width_in:.1f} inches
- Background: white or off-white (#FAFAFA)"""

        # Generate image
        _log(f"[Visualizer] Generating image (round {round_idx + 1}/{max_critic_rounds})...")
        ok = generate_figure(image_prompt, output_path, api_key=key, model=model,
                             aspect_ratio=aspect_ratio, receipt=receipt)
        if not ok:
            _log(f"Image generation failed in round {round_idx + 1}", "WARN")
            break

        generated = True
        if max_critic_rounds == 0:
            break

        # Read generated image for Critic
        try:
            image_bytes = output_path.read_bytes()
        except Exception:
            _log("Failed to read generated image for critic", "WARN")
            break

        # Run Critic
        _log(f"[Critic] Evaluating (round {round_idx + 1}/{max_critic_rounds})...")
        eval_result = _run_critic(client, text_model, styled_description,
                                  image_bytes, caption, style_guide)

        f_score = eval_result.get("faithfulness", 0)
        c_score = eval_result.get("conciseness", 0)
        r_score = eval_result.get("readability", 0)
        a_score = eval_result.get("aesthetics", 0)
        p_score = eval_result.get("publication_readiness", 0)
        overall = eval_result.get("overall", 0)
        suggestions = eval_result.get("critic_suggestions", "")

        _log(f"  Scores: F={f_score} C={c_score} R={r_score} A={a_score} P={p_score} Overall={overall}")
        if suggestions and suggestions != "No changes needed.":
            _log(f"  Suggestions: {suggestions[:200]}")

        # Check if we're done — require BOTH overall >= 4 AND publication_readiness >= 4
        if suggestions == "No changes needed." or (overall >= 4 and p_score >= 4):
            _log(f"[Critic] Approved! (overall={overall}, pub_readiness={p_score})")
            break

        # Use revised description for next round
        revised = eval_result.get("revised_description", "")
        if revised and revised.strip():
            styled_description = revised
        elif suggestions and suggestions.strip():
            # Critic gave suggestions but no revised description — append suggestions
            # to the existing description so the next round can incorporate them
            styled_description = f"""{styled_description}

IMPORTANT CORRECTIONS (from quality review):
{suggestions}

Apply ALL the corrections above. Fix every issue mentioned."""
            _log("Critic gave suggestions without revised description, appending to spec", "INFO")
        else:
            _log("Critic gave no actionable feedback, stopping", "WARN")
            break

    if generated:
        _log(f"Pipeline complete: {output_path.name}")
    else:
        _log(f"Pipeline failed for {figure_name}", "WARN")
    return generated


# ══════════════════════════════════════════════════════════════
#  CLI Entry Point (for standalone testing)
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Nano Banana: AI figure generation for academic papers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # One-shot generation
  python -m ark.nano_banana --prompt "Neural network architecture diagram" --output /tmp/test.png

  # Full pipeline (Planner → Stylist → Critic loop)
  python -m ark.nano_banana --pipeline --name "fig_overview" \\
    --caption "System architecture overview" \\
    --context "Our system uses a multi-agent pipeline..." \\
    --output /tmp/test_pipeline.png --model flash
""")
    parser.add_argument("--prompt", help="Direct prompt for one-shot generation")
    parser.add_argument("--pipeline", action="store_true", help="Use full Planner→Stylist→Critic pipeline")
    parser.add_argument("--name", default="test_figure", help="Figure name (pipeline mode)")
    parser.add_argument("--caption", default="", help="Figure caption (pipeline mode)")
    parser.add_argument("--context", default="", help="Paper context (pipeline mode)")
    parser.add_argument("--context-file", help="Read paper context from file")
    parser.add_argument("--output", default="nano_banana_output.png", help="Output PNG path")
    parser.add_argument("--model", default="flash", choices=["flash", "pro"])
    parser.add_argument("--max-rounds", type=int, default=3, help="Max critic rounds (pipeline mode)")
    parser.add_argument("--venue", default="", help="Target venue")
    parser.add_argument("--width", type=float, default=3.333, help="Column width in inches")
    args = parser.parse_args()

    # Read context from file if specified. Gemini 2.x handles large contexts
    # natively — we don't pre-truncate here. generate_figure_pipeline calls
    # Gemini, which does not have a Read tool, so the context must travel as
    # a string. If cost or latency becomes a problem, gate at call time with
    # an explicit, generous cap, not a silent 5000-char slice that drops the
    # second half of a finding.
    context = args.context
    if args.context_file:
        context = Path(args.context_file).read_text()

    def log_fn(msg, level="INFO"):
        print(f"[{level}] {msg}")

    if args.pipeline:
        ok = generate_figure_pipeline(
            figure_name=args.name,
            caption=args.caption or args.name,
            paper_context=context,
            output_path=Path(args.output),
            model=args.model,
            venue=args.venue,
            column_width_in=args.width,
            max_critic_rounds=args.max_rounds,
            log_fn=log_fn,
        )
    else:
        prompt = args.prompt or "A professional scientific concept diagram"
        ok = generate_figure(prompt, Path(args.output), model=args.model)

    print(f"\n{'Success' if ok else 'Failed'}: {args.output}")
