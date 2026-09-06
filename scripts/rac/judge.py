"""Score a paper PDF with two independent reviewers and record every dimension.

Two reviewers, because a single scalar cannot say *where* an arm differs:

  ark     — ARK's own reviewer prompt (ark/templates/agents/reviewer.prompt),
            four weighted dimensions on a 1-10 scale. This is the same rubric
            the in-loop reviewer applies, so it is comparable to the scores in
            paper_state.yaml — but for the runtime arm that rubric is also the
            stopping condition, so it is reported apart from the others.
  sakana  — the NeurIPS form from SakanaAI/AI-Scientist's perform_review.py,
            seven 1-4 dimensions plus Overall, Confidence, and a binary
            decision. Run twice, once under each of that file's two system
            prompts, because the negative and positive variants bracket the
            same paper and a single side would carry its bias into the delta.

Both run on text extracted from the PDF, so a missing figure lowers the
presentation dimensions the way it would for a reader who cannot see it.

    python scripts/rac/judge.py --pdf a.pdf --label baseline --out judgements/
    python scripts/rac/judge.py --pdf-dir judgements/blind --out judgements/
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

import yaml

ARK_ROOT = pathlib.Path(__file__).resolve().parents[2]
MODEL = "claude-sonnet-4-6"
MAX_PAPER_CHARS = 120_000          # ~30k tokens; the papers here run 11-12 pages
API = "https://api.anthropic.com/v1/messages"


# ── PDF text ────────────────────────────────────────────────────────────────
def pdf_text(path: pathlib.Path) -> str:
    """Extract text, preferring the extractor that keeps reading order."""
    try:
        import pymupdf4llm
        return pymupdf4llm.to_markdown(str(path))
    except Exception:
        pass
    try:
        import fitz
        return "\n".join(p.get_text() for p in fitz.open(str(path)))
    except Exception:
        pass
    import pypdf
    return "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(str(path)).pages)


# ── Anthropic ───────────────────────────────────────────────────────────────
def api_key() -> str:
    cfg = yaml.safe_load((ARK_ROOT / ".ark/config.yaml").read_text())
    return cfg["anthropic_api_key"]


def ask(system: str, user: str, key: str, max_tokens: int = 8000,
        retries: int = 3) -> tuple[str, dict]:
    body = {"model": MODEL, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}]}
    for attempt in range(retries):
        req = urllib.request.Request(
            API, data=json.dumps(body).encode(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=600) as response:
                payload = json.loads(response.read())
            return payload["content"][0]["text"], payload.get("usage", {})
        except urllib.error.HTTPError as error:
            detail = error.read().decode()[:200]
            if attempt == retries - 1:
                raise RuntimeError(f"HTTP {error.code}: {detail}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


# ── Reviewer 1: ARK's own rubric ────────────────────────────────────────────
ARK_DIMENSIONS = ("Technical Quality", "Paper Presentation", "Innovation",
                  "Writing Quality")


def ark_review(text: str, key: str) -> dict:
    """ARK's reviewer prompt, with its file-writing steps removed.

    The prompt tells an agent to read the project and save a report; here the
    paper arrives inline and only the rubric and output format are wanted, so
    the surrounding instructions would otherwise ask for actions this reviewer
    cannot take.
    """
    prompt = (ARK_ROOT / "ark/templates/agents/reviewer.prompt").read_text()
    system = (
        "You are the reviewer described below. The paper is supplied inline as "
        "extracted text, so ignore any instruction to read project files, run "
        "commands, or save a report. Judge only what the text shows; figures "
        "are described by their captions and any missing figure counts against "
        "the presentation dimension. Reply with the review in the requested "
        "format, including the scores table and the `Overall Score: N/10` line."
        f"\n\n---\n{prompt}"
    )
    out, usage = ask(system, f"## Paper under review\n\n{text[:MAX_PAPER_CHARS]}", key)
    scores = {}
    for dimension in ARK_DIMENSIONS:
        match = re.search(rf"{re.escape(dimension)}\s*\|\s*(\d+(?:\.\d+)?)\s*/\s*10", out)
        if match:
            scores[dimension] = float(match.group(1))
    overall = re.search(r"Overall Score[:：]\s*(\d+(?:\.\d+)?)\s*/\s*10", out, re.I)
    total = re.search(r"\*\*Total\*\*\s*\|\s*\*\*(\d+(?:\.\d+)?)\s*/\s*10", out)
    return {"reviewer": "ark", "dimensions": scores,
            "overall": float(overall.group(1)) if overall
                       else (float(total.group(1)) if total else None),
            "usage": usage, "raw": out}


# ── Reviewer 2: Sakana's NeurIPS form ───────────────────────────────────────
SAKANA_NUMERIC = (("Originality", 4), ("Quality", 4), ("Clarity", 4),
                  ("Significance", 4), ("Soundness", 4), ("Presentation", 4),
                  ("Contribution", 4), ("Overall", 10), ("Confidence", 5))


def sakana_prompts() -> tuple[str, str, str]:
    """The two system prompts and the review form, read from the real file."""
    source = (ARK_ROOT / "submodules/sakana-review/perform_review.py").read_text()

    def literal(name: str) -> str:
        """Concatenate the string pieces of a parenthesised assignment."""
        match = re.search(rf'^{name}\s*=\s*\(\s*(.*?)\n\)', source, re.S | re.M)
        if not match:
            raise RuntimeError(f"could not read {name} from perform_review.py")
        body = match.group(1)
        parts = re.findall(r'"""(.*?)"""', body, re.S) or \
                re.findall(r'"((?:[^"\\]|\\.)*)"', body)
        return "".join(parts).encode().decode("unicode_escape")

    def triple(name: str) -> str:
        match = re.search(rf'^{name}\s*=\s*"""(.*?)"""', source, re.S | re.M)
        if not match:
            raise RuntimeError(f"could not read {name} from perform_review.py")
        return match.group(1)

    # The two variants are the shared base plus one appended sentence, so the
    # base has to be prepended or each reviewer loses its framing. And
    # `neurips_form` is the rubric *plus* `template_instructions`, which is the
    # part that asks for the JSON block — without it the reviewer answers in
    # prose and every dimension parses as missing.
    base = literal("reviewer_system_prompt_base")
    form = literal("neurips_form") + triple("template_instructions")
    return (base + literal("reviewer_system_prompt_neg"),
            base + literal("reviewer_system_prompt_pos"),
            form)


def sakana_review(text: str, key: str, bias: str) -> dict:
    neg, pos, form = sakana_prompts()
    system = {"negative": neg, "positive": pos}[bias]
    user = (f"{form}\n\nHere is the paper you are asked to review:\n"
            f"```\n{text[:MAX_PAPER_CHARS]}\n```")
    out, usage = ask(system, user, key)
    blob = re.search(r"\{.*\}", out, re.S)
    parsed = {}
    if blob:
        try:
            parsed = json.loads(blob.group(0))
        except json.JSONDecodeError:
            pass
    scores = {}
    for name, _ in SAKANA_NUMERIC:
        if name in parsed:
            try:
                scores[name] = float(parsed[name])
            except (TypeError, ValueError):
                pass
        else:
            match = re.search(rf'"{name}"\s*:\s*(\d+(?:\.\d+)?)', out)
            if match:
                scores[name] = float(match.group(1))
    return {"reviewer": f"sakana-{bias}", "dimensions": scores,
            "overall": scores.get("Overall"),
            "decision": parsed.get("Decision"),
            "usage": usage, "raw": out}


# ── Driver ──────────────────────────────────────────────────────────────────
def judge(pdf: pathlib.Path, label: str, out_dir: pathlib.Path,
          key: str) -> dict:
    text = pdf_text(pdf)
    print(f"  {label}: {len(text)} chars of text from {pdf.name}", flush=True)
    result = {"label": label, "pdf": str(pdf), "chars": len(text),
              "reviews": []}
    for name, fn in (("ark", lambda: ark_review(text, key)),
                     ("sakana-negative", lambda: sakana_review(text, key, "negative")),
                     ("sakana-positive", lambda: sakana_review(text, key, "positive"))):
        started = time.time()
        try:
            review = fn()
            print(f"    {name:18} overall={review['overall']}  "
                  f"dims={review['dimensions']}  ({time.time()-started:.0f}s)", flush=True)
        except Exception as error:
            review = {"reviewer": name, "error": f"{type(error).__name__}: {error}"}
            print(f"    {name:18} FAILED {review['error']}", flush=True)
        result["reviews"].append(review)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{label}.json").write_text(json.dumps(result, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", action="append", default=[],
                        help="PDF to judge; repeatable, paired with --label")
    parser.add_argument("--label", action="append", default=[])
    parser.add_argument("--out", default=str(ARK_ROOT / "scripts/rac/judgements"))
    args = parser.parse_args()
    if len(args.pdf) != len(args.label):
        print("error: one --label per --pdf", file=sys.stderr)
        return 1

    key = api_key()
    out_dir = pathlib.Path(args.out)
    for pdf, label in zip(args.pdf, args.label):
        path = pathlib.Path(pdf)
        if not path.exists():
            print(f"  {label}: missing {path}", file=sys.stderr)
            continue
        judge(path, label, out_dir, key)
    print(f"\nwrote {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
