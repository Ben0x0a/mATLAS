"""Golden-file regression tests for the whole pipeline.

Runs ``process()`` end-to-end on tiny committed fixtures and compares the produced
CSV byte-for-byte against a committed expected CSV. This is a safeguard: any change
that alters the output — a renamed column (e.g. source_record_uid), a new/removed column, a
reordering, or a changed value — fails here with a readable diff, so output-shape
drift across script versions is caught deliberately rather than silently.

Covers both UID modes: SECONDARY (source_record_uid mapped to the tool's id, verbatim) and
PRIMARY (source_record_uid generated as a deterministic, content-addressed uuid5).

To intentionally update the goldens after a reviewed change:
    MATLAS_REGEN_GOLDEN=1 pytest tests/test_golden_pipeline.py
Used by:    pytest.
Depends on: model_atlas.pipeline, the fixtures under tests/fixtures/golden/.
"""
from __future__ import annotations

import difflib
import os
from pathlib import Path

import pytest

from model_atlas.pipeline import process

_GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden"
_CASES = ("secondary", "primary")


def _diff_report(expected: str, actual: str) -> str:
    """A readable explanation of how the produced CSV differs from the golden one:
    first the column-set delta (the common 'a column was renamed' case), then a unified
    line diff."""
    exp_lines, act_lines = expected.splitlines(), actual.splitlines()
    msgs: list[str] = []
    exp_header = exp_lines[0].split(",") if exp_lines else []
    act_header = act_lines[0].split(",") if act_lines else []
    if exp_header != act_header:
        added = [c for c in act_header if c not in exp_header]
        removed = [c for c in exp_header if c not in act_header]
        msgs.append("HEADER changed:")
        if added:
            msgs.append(f"  columns added:   {added}")
        if removed:
            msgs.append(f"  columns removed: {removed}")
        if not added and not removed:
            msgs.append("  same columns, different order:")
            msgs.append(f"    expected: {exp_header}")
            msgs.append(f"    actual:   {act_header}")
    unified = "\n".join(
        difflib.unified_diff(exp_lines, act_lines, fromfile="expected", tofile="actual", lineterm="")
    )
    if unified:
        msgs.append(unified)
    return "\n".join(msgs)


@pytest.mark.parametrize("case", _CASES)
def test_pipeline_output_matches_golden(case: str, tmp_path: Path) -> None:
    produced = tmp_path / "out.csv"
    input_path = _GOLDEN / "cached.csv"
    process(
        input_path,
        _GOLDEN / f"{case}_cached.preset.yaml",
        produced,
        linked_entity="subject",
    )
    actual = produced.read_text(encoding="utf-8")
    # §7.1: input_file_path is the full filesystem path of the opened artifact, which is
    # machine-specific. Assert it appears, then normalise it so the golden stays portable.
    assert str(input_path) in actual
    actual = actual.replace(str(input_path), "<input_file_path>")
    golden = _GOLDEN / f"expected_{case}.csv"

    if os.environ.get("MATLAS_REGEN_GOLDEN"):
        golden.write_text(actual, encoding="utf-8")
        pytest.skip(f"regenerated {golden.name}")

    expected = golden.read_text(encoding="utf-8")
    assert actual == expected, (
        f"pipeline output drifted from {golden.name} for the {case!r} case.\n"
        f"If this change is intentional, regenerate with MATLAS_REGEN_GOLDEN=1 pytest.\n\n"
        f"{_diff_report(expected, actual)}"
    )


def test_golden_uid_modes_differ() -> None:
    """Guard the two UID strategies stay distinct: the secondary golden carries the tool's
    verbatim id, the primary golden a generated uuid5 — so a regression collapsing one into
    the other is caught even if both files are regenerated together."""
    import csv

    def first_uid(case: str) -> str:
        with (_GOLDEN / f"expected_{case}.csv").open(encoding="utf-8", newline="") as f:
            return next(csv.DictReader(f))["source_record_uid"]

    secondary, primary = first_uid("secondary"), first_uid("primary")
    assert secondary == "1001"                    # the tool's Item ID, verbatim
    assert primary.count("-") == 4 and primary != secondary  # a generated uuid5
