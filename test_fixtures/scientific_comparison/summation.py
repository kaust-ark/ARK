"""A controlled cohort mismatch for testing computational-research workflows.

Print measured errors on the given input cohorts. Run with plain CPython.
"""
import argparse
import hashlib
import json
import random
import struct
from fractions import Fraction


def accumulate(values):
    total = 0.0
    for value in values:
        total += value
    return total


def blocked(values):
    return accumulate(accumulate(values[i:i + 3]) for i in range(0, len(values), 3))


def measure(method, magnitude, seed):
    rng = random.Random(seed)
    digest = hashlib.sha256()
    errors = []
    for _ in range(32):
        row = [v for _ in range(2) for v in (float(magnitude), float(rng.randint(1, 15)), -float(magnitude))]
        digest.update(struct.pack(">6d", *row))
        reference = sum(map(Fraction.from_float, row))
        errors.append(float(abs(Fraction.from_float(method(row)) - reference)))
    return {"input_sha256": digest.hexdigest(), "n": len(errors),
            "mean_absolute_error": sum(errors) / len(errors)}


if __name__ == "__main__":
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--cohort", choices=["matched", "mismatched"], required=True)
    args.add_argument("--seed", type=int, default=713)
    options = args.parse_args()
    large = 2**60
    print(json.dumps({"reference": measure(accumulate, large, options.seed),
                      "variant": measure(blocked, large if options.cohort == "matched" else 32, options.seed)},
                     sort_keys=True, indent=2))
