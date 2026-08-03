"""Tests for the entity / concerns autocompletion candidates (gui.completion).

Covers the keyword templates, the single-CSV column peek (present only for a lone .csv
file), and the guard that folders / non-CSV / missing paths get keywords alone.
Used by:    pytest.
Depends on: gui.completion (no Qt dependency, so runnable headless).
"""
from __future__ import annotations

from pathlib import Path

from gui.completion import PARAM_KEYWORDS, param_completions

_CSV = "Record,Latitude,Evidence number\n1,46.5,EV-1\n2,47.0,EV-2\n"


def test_keyword_templates_are_the_param_expr_kinds() -> None:
    # The offered keywords are exactly the call-form kinds a run-level param may be.
    assert set(PARAM_KEYWORDS) == {"const(", "field(", "filename(", "header(", "preset("}


def test_single_csv_offers_field_per_column(tmp_path: Path) -> None:
    csv = tmp_path / "locations.csv"
    csv.write_text(_CSV, encoding="utf-8")
    candidates = param_completions(str(csv))
    # Keyword templates plus one field(<column>) per CSV header.
    assert set(PARAM_KEYWORDS) <= set(candidates)
    assert "field(Evidence number)" in candidates
    assert "field(Record)" in candidates and "field(Latitude)" in candidates


def test_semicolon_and_tab_delimiters_are_sniffed(tmp_path: Path) -> None:
    # Excel regional (semicolon) and tab-delimited CSVs list their columns correctly, so a
    # comma-only assumption never collapses the header into one field.
    for suffix, sep in (("semi", ";"), ("tab", "\t")):
        csv = tmp_path / f"loc_{suffix}.csv"
        csv.write_text(sep.join(["Record", "Latitude", "Evidence number"]) + f"\n1{sep}46.5{sep}EV-1\n", encoding="utf-8")
        candidates = param_completions(str(csv))
        assert "field(Evidence number)" in candidates
        assert "field(Record)" in candidates and "field(Latitude)" in candidates


def test_folder_and_missing_and_non_csv_get_keywords_only(tmp_path: Path) -> None:
    excel = tmp_path / "book.xlsx"
    excel.write_bytes(b"not really excel")
    for source in (None, "", str(tmp_path), str(tmp_path / "nope.csv"), str(excel)):
        assert param_completions(source) == list(PARAM_KEYWORDS)
