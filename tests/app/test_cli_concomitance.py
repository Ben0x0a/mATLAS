"""Tests for the ``concomitance`` subcommand in launcher/cli.py (the T101b Verify list).

Covers:     ``--list-entities`` prints TSV on stdout and exits 0 without running a query,
            matching ``read_case_vocabulary``; a malformed ``--group1`` (unknown kind,
            missing ``=``) exits non-zero naming the allowed kinds, never a traceback;
            ``--before``/``--after`` duration parsing shares ``DURATION_UNIT_NAMES``/
            ``duration_from`` with the GUI, both via model_atlas.analyse.concomitance
            (the same object, not a copy);
            a supported request writes the T101a CSV contract and exits 0;
            ``check_request`` ERRORs exit 1 and the identical-groups WARNING does not
            block. (The four "not yet supported" shapes this file once asserted were
            removed when the T102 engine landed and made all of them real.)
Uses:       ``launcher.cli.run`` (the real entry point — argv in, exit code out) and the
            same fixture-building helper as tests/app/test_concomitance_run.py, so the
            CLI is exercised exactly as an operator would invoke it.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from launcher import cli
from model_atlas.analyse.helpers import read_case_vocabulary
from model_atlas.analyse.concomitance import CSV_HEADER
from model_atlas.analyse.helpers import CriterionKind
from gui import spatiotemporal_query as gui_seam
from model_atlas.analyse.concomitance import DURATION_UNIT_NAMES, duration_from
from tests.app.test_concomitance_run import KNOWN, _build_case, _north_of


def _run(argv: list[str]) -> int:
    return cli.run(argv)


# --- duration parsing: one shared unit table, owned by the request layer -------------

def test_duration_arg_shares_the_one_unit_table() -> None:
    # Same function object and same name tuple, not a re-implementation: the CLI's
    # --before/--after suffixes and the GUI's unit combos can never disagree on what a
    # unit means. Both reach it through model_atlas.analyse.concomitance, which
    # is where it is DEFINED — a shell importing another shell to share it would invert
    # the dependency direction.
    assert cli.duration_from is duration_from
    assert cli.DURATION_UNIT_NAMES is DURATION_UNIT_NAMES
    assert gui_seam.duration_from is duration_from


def test_duration_arg_accepts_every_documented_suffix() -> None:
    assert cli._parse_duration_arg("30s") == dt.timedelta(seconds=30)
    assert cli._parse_duration_arg("10m") == dt.timedelta(minutes=10)
    assert cli._parse_duration_arg("2h") == dt.timedelta(hours=2)
    assert cli._parse_duration_arg("1d") == dt.timedelta(days=1)


def test_duration_arg_rejects_a_bare_number() -> None:
    with pytest.raises(cli.argparse.ArgumentTypeError):
        cli._parse_duration_arg("30")


def test_duration_arg_rejects_an_unknown_suffix() -> None:
    with pytest.raises(cli.argparse.ArgumentTypeError, match="unknown duration unit"):
        cli._parse_duration_arg("30w")


# --- --list-entities ---------------------------------------------------------------------

def test_list_entities_prints_tsv_and_exits_zero_without_running_a_query(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    _build_case(db_path)

    exit_code = _run(["concomitance", "--case", str(db_path),
                       "--list-entities", "identifier"])

    assert exit_code == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == "value\tqualifier\tplacement_count"
    expected = read_case_vocabulary(db_path)[CriterionKind.IDENTIFIER]
    assert len(lines) == 1 + len(expected)
    for line, entry in zip(lines[1:], expected):
        value, qualifier, count = line.split("\t")
        assert value == entry.value
        assert qualifier == (entry.qualifier or "")
        assert int(count) == entry.placement_count


def test_list_entities_on_a_missing_case_exits_nonzero_not_a_traceback(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "does-not-exist.matlas.sqlite"
    exit_code = _run(["concomitance", "--case", str(missing), "--list-entities", "entity_type"])
    assert exit_code == 1
    assert "Traceback" not in capsys.readouterr().err


# --- malformed --group1 ---------------------------------------------------------------

def test_malformed_group1_missing_equals_exits_nonzero_naming_allowed_kinds(
        capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _run(["concomitance", "--case", "case.matlas.sqlite", "--group1", "identifier"])
    assert excinfo.value.code != 0
    err = capsys.readouterr().err
    assert "Traceback" not in err
    for kind in CriterionKind:
        assert kind.value in err


def test_malformed_group1_unknown_kind_exits_nonzero_naming_allowed_kinds(
        capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _run(["concomitance", "--case", "case.matlas.sqlite",
              "--group1", "not_a_kind=351324466102918"])
    assert excinfo.value.code != 0
    err = capsys.readouterr().err
    assert "Traceback" not in err
    for kind in CriterionKind:
        assert kind.value in err


# --- a supported request writes the CSV -------------------------------------------------

def test_supported_request_writes_csv_and_exits_zero(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    a, b = _build_case(db_path)
    out_path = tmp_path / "results.csv"

    exit_code = _run([
        "concomitance", "--case", str(db_path),
        "--distance", "50", "--before", "10m", "--after", "10m",
        "--output", str(out_path),
    ])

    assert exit_code == 0
    raw = out_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")   # utf-8-sig, house rule
    lines = raw.decode("utf-8-sig").splitlines()
    assert lines[0] == ",".join(CSV_HEADER)
    assert len(lines) == 2
    row = lines[1].split(",")
    assert {row[0], row[10]} == {"+41790000001", "+41790000002"}
    assert {row[8], row[18]} == {a, b}


def test_check_request_error_exits_one_before_running(capsys: pytest.CaptureFixture[str],
                                                       tmp_path: Path) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    _build_case(db_path)
    out_path = tmp_path / "results.csv"

    exit_code = _run([
        "concomitance", "--case", str(db_path),
        "--distance", "-1", "--before", "10m", "--after", "10m",
        "--output", str(out_path),
    ])

    assert exit_code == 1
    assert not out_path.exists()
    err = capsys.readouterr().err
    assert "Distance threshold must be 0 m or more." in err
    assert "Traceback" not in err


def test_identical_groups_warning_does_not_block_the_run(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    _build_case(db_path)
    out_path = tmp_path / "results.csv"

    # Both groups default to "every entity" -> same_selection is True -> the WARNING
    # fires, but the run must still proceed and write a file.
    exit_code = _run([
        "concomitance", "--case", str(db_path),
        "--distance", "50", "--before", "10m", "--after", "10m",
        "--output", str(out_path),
    ])

    assert exit_code == 0
    assert out_path.is_file()
    err = capsys.readouterr().err
    assert "same entities in one case" in err


# =========================================================================================
