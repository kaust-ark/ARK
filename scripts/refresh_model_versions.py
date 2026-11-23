#!/usr/bin/env python3
"""Compatibility command for installations with the former model refresh timer."""

import argparse


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("--check", "--apply", "--notify"):
        parser.add_argument(flag, action="store_true", help="accepted for older installations")
    parser.parse_args(argv)
    print("Idea2Paper now refreshes the model catalog automatically from each account’s configured providers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
