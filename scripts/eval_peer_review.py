"""Prepare opaque reviewer qualification packets and score independent adjudication.

  python scripts/eval_peer_review.py prepare /tmp/packets /tmp/jury-key.json --split calibration
  python scripts/eval_peer_review.py grade /tmp/jury-key.json /tmp/baseline-reviews /tmp/baseline-jury.json

Prepare the final held-out topic separately using --split holdout. Give each reviewer
only the individual project and its required evidence domains. Use identical packets,
model and budgets for both arms. Keep the key, other variants, reviews and rubric
answers out of reviewer sessions. The excerpts are original, compact qualifications,
not previously peer-reviewed papers or new experimental data.

Save each blind review as ID.md or ID.json. An independent reader adjudicates each
review against the key, recording a JSON object keyed by ID. Each entry binds its
review_sha256 and gives a short note explaining the judgment. For flawed cases
supply booleans defect_found and evidence_located. For clean controls supply
unsupported_block. For withheld cases supply abstained. The grader computes counts
and denominators, never treats word matching as scientific evaluation.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import secrets
import string
import subprocess

FIXTURES = Path(__file__).resolve().parents[1] / "test_fixtures/peer_review/pairs.json"


def prepare(output, key_path, split, seed=None):
    output = output.resolve()
    key_path = key_path.resolve()
    if key_path == output or key_path.is_relative_to(output):
        raise ValueError("Keep the private jury key outside the reviewers' packet directory")
    if key_path.exists() or output.exists() and any(output.iterdir()):
        raise ValueError("Use a new jury key and empty packet directory")
    pairs = json.loads(FIXTURES.read_text())
    cases = [(p, variant) for p in pairs if split == "all" or (p["topic"] == "timing") == (split == "holdout")
             for variant in ("clean", "flawed", "withheld")]
    rng = random.Random(seed if seed is not None else secrets.token_bytes(32))
    rng.shuffle(cases)
    key = {}
    output.mkdir(parents=True, exist_ok=True)
    for pair, variant in cases:
        ident = "".join(rng.choices(string.ascii_lowercase + string.digits, k=12))
        root = output / ident
        paper = root / "paper"
        paper.mkdir(parents=True)
        claim = pair["clean" if variant == "withheld" else variant]
        tex = (
            "\\documentclass{article}\n\\usepackage[T1]{fontenc}\n\\begin{document}\n"
            + "\\section*{" + pair["title"] + "}\n"
            + pair["question"] + "\n\n" + claim + "\n\\end{document}\n"
        )
        (paper / "main.tex").write_text(tex)
        subprocess.run(["pdflatex", "-no-shell-escape", "-halt-on-error", "-interaction=batchmode", "main.tex"],
                       cwd=paper, stdout=subprocess.DEVNULL, check=True, timeout=30)
        for suffix in ("aux", "log"):
            (paper / f"main.{suffix}").unlink(missing_ok=True)
        if variant != "withheld":
            source, data = (("sources/study.txt", pair["source"]) if "source" in pair
                            else ("results/rows.csv", pair["table"]))
            asset = root / source
            asset.parent.mkdir()
            asset.write_text(data)
        (root / "rubric.json").write_text(json.dumps({
            "target_track": "short reproducibility exercise", "required": ["manuscript", pair["domain"]]
        }) + "\n")
        key[ident] = {"topic": pair["topic"], "variant": variant, "claim": claim,
                      "required_domain": pair["domain"], "defect": pair["defect"]}
    key_path.parent.mkdir(parents=True, exist_ok=True)
    with key_path.open("x") as f:
        json.dump(key, f, indent=2)
    return {"packets": len(key), "split": split}


def grade(key, reviews, jury):
    if set(jury) != set(key):
        raise ValueError("The jury must adjudicate every frozen packet exactly once")
    metrics = {m: [0, 0] for m in ("critical_defects_found", "correct_evidence_locations",
                                     "clean_controls_incorrectly_blocked", "withheld_cases_abstained")}
    for ident, truth in key.items():
        entries = [reviews / (ident + s) for s in (".md", ".json") if (reviews / (ident + s)).is_file()]
        if len(entries) != 1:
            raise ValueError(f"Expected exactly one blind review for {ident}")
        assessment = jury[ident]
        if assessment.get("review_sha256") != hashlib.sha256(entries[0].read_bytes()).hexdigest():
            raise ValueError(f"The adjudication refers to a different review for {ident}")
        if not isinstance(assessment.get("note"), str) or not assessment["note"].strip():
            raise ValueError(f"Missing jury rationale for {ident}")
        mapping = {"flawed": {"defect_found": "critical_defects_found", "evidence_located": "correct_evidence_locations"},
                   "clean": {"unsupported_block": "clean_controls_incorrectly_blocked"},
                   "withheld": {"abstained": "withheld_cases_abstained"}}[truth["variant"]]
        if (truth["variant"] == "flawed" and assessment.get("evidence_located") and not assessment.get("defect_found")):
            raise ValueError(f"Evidence for an undetected defect cannot receive credit: {ident}")
        for field, metric in mapping.items():
            if type(assessment.get(field)) is not bool:
                raise ValueError(f"Expected a Boolean {field} adjudication for {ident}")
            metrics[metric][0] += int(assessment[field])
            metrics[metric][1] += 1
    passed = all(n == (0 if metric == "clean_controls_incorrectly_blocked" else d) and d
                 for metric, (n, d) in metrics.items())
    return {"pass": passed, "metrics": {m: {"count": n, "total": d} for m, (n, d) in metrics.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="op", required=True)
    packets = sub.add_parser("prepare")
    packets.add_argument("output", type=Path)
    packets.add_argument("key", type=Path)
    packets.add_argument("--split", choices=("calibration", "holdout", "all"), default="calibration")
    packets.add_argument("--seed", type=int, help="Use only when reproducing a previous blinded ordering")
    adjudicate = sub.add_parser("grade")
    adjudicate.add_argument("key", type=Path)
    adjudicate.add_argument("reviews", type=Path)
    adjudicate.add_argument("jury", type=Path)
    args = parser.parse_args()
    if args.op == "prepare":
        result = prepare(args.output, args.key, args.split, args.seed)
    else:
        result = grade(json.loads(args.key.read_text()), args.reviews, json.loads(args.jury.read_text()))
    print(json.dumps(result, indent=2))
    return int(result.get("pass") is False)


if __name__ == "__main__":
    raise SystemExit(main())
