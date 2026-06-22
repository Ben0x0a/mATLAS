"""Command-line preset linter.

Runs the package linter (``model_atlas.presets.lint``) over one or more preset files
or folders and prints grouped findings. Exits non-zero when any ERROR is found, so it
is usable as a CI gate.

Usage:
    python utils/lint_presets.py [paths ...]          # default: ./presets
    python utils/lint_presets.py presets/ios --advice # include best-practice advice
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_atlas.presets.lint import ADVICE, ERROR, WARNING, lint_paths  # noqa: E402

_ORDER = {ERROR: 0, WARNING: 1, ADVICE: 2}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, default=[Path("presets")],
                        help="Preset files or folders to lint (default: ./presets).")
    parser.add_argument("--advice", action="store_true",
                        help="Include best-practice ADVICE findings (off by default).")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero if any WARNING (not just ERROR) is found.")
    args = parser.parse_args(argv)

    findings = lint_paths(args.paths or [Path("presets")])
    if not args.advice:
        findings = [f for f in findings if f.severity != ADVICE]

    by_preset: dict[str, list] = {}
    for finding in findings:
        by_preset.setdefault(finding.preset, []).append(finding)

    counts = {ERROR: 0, WARNING: 0, ADVICE: 0}
    for preset in sorted(by_preset):
        print(preset)
        for finding in sorted(by_preset[preset], key=lambda f: _ORDER.get(f.severity, 9)):
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
            print(f"  {finding.format()}")
    print(f"\n{counts[ERROR]} error(s), {counts[WARNING]} warning(s), {counts[ADVICE]} advice")

    if counts[ERROR] or (args.strict and counts[WARNING]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
