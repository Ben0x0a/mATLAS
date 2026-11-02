"""A changed report must still dump completely; what cannot be read must raise an error, never pass quietly.

Defines:    the table of structural changes a Cellebrite update could bring, each with the outcome it
            must produce, run single-process and with parse workers; plus the model-tag counter.
Used by:    pytest.
Depends on: ufdr_parser (dump, integrity, scan), the test_workbook report.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest
from openpyxl import load_workbook

from tests.ufdr_parser.test_workbook import REPORT
from ufdr_parser.const import PARENT_FIELD_COLNAME, TRACE_COLUMNS
from ufdr_parser.dump import run_dump
from ufdr_parser.integrity import ReportIntegrityError
from ufdr_parser.scan import ModelTagCounter

_DECLARED = REPORT.replace('name="wb"', 'name="wb" ModelCount="43"')


def _renamed(old: str, new: str):
    """Rename an element: its start tag (with attributes) and its end tag."""
    return lambda xml: xml.replace(f"<{old} ", f"<{new} ").replace(f"<{old}>", f"<{new}>").replace(f"</{old}>", f"</{new}>")


def _text(old: str, new: str):
    return lambda xml: xml.replace(old, new)


# The structure around the models changed: every record and value must still come out, whole.
_ABSORBED = {
    "modelType renamed": _renamed("modelType", "modelGroup"),
    "decodedData renamed": _renamed("decodedData", "decoded"),
    "field renamed": _renamed("field", "attr"),
    "value renamed": _renamed("value", "val"),
    "modelField renamed": _renamed("modelField", "subModel"),
    "multiModelField renamed": _renamed("multiModelField", "listField"),
}
# Nothing can be trusted: stop, no workbook, whatever CSVs were written stay.
_STOPS = {
    "model renamed": (_renamed("model", "record"), "No <model> element could be read"),
    "truncated": (lambda xml: xml[: len(xml) * 2 // 3], "does not end properly"),
}
# A part did not reach the output: the workbook is written, and the error is raised.
_ERRORS = {
    "Location renamed": (_text('type="Location"', 'type="GeoPoint"'), "Journeys hold coordinates"),
    "Coordinate renamed": (_text('type="Coordinate"', 'type="GeoCoord"'), "none has a Coordinate"),
    "Latitude renamed": (_text('name="Latitude"', 'name="Lat"'), "no Latitude field"),
    "Party renamed": (_text('type="Party"', 'type="Participant"'), "has a sender"),
    "Party.Identifier renamed": (_text('name="Identifier"', 'name="Handle"'), "no Identifier field"),
    "Role value changed": (_text("[From]", "[Sender]"), "has a sender"),
    "InstantMessage renamed": (_text('type="InstantMessage"', 'type="Message"'), "does not read as messages"),
    "Call renamed": (_text('type="Call"', 'type="PhoneCall"'), "not on the communication sheets"),
    "invalid XML mid-file": (lambda xml: xml[: len(xml) // 2] + "<<<&&& not xml >>>" + xml[len(xml) // 2 :], "lost by the XML parser"),
}


@pytest.fixture(autouse=True)
def _no_local_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # the built-in ufdr_config.toml, whatever the working directory


def _report(tmp_path: Path, xml: str) -> Path:
    path = tmp_path / "r.xml"
    path.write_text(xml, encoding="utf-8")
    return path


@pytest.mark.parametrize("parse_workers", [1, 2])
@pytest.mark.parametrize("name", _STOPS)
def test_report_not_understood_stops(tmp_path: Path, name: str, parse_workers: int) -> None:
    change, message = _STOPS[name]
    with pytest.raises(ReportIntegrityError, match=message):
        run_dump(_report(tmp_path, change(_DECLARED)), tmp_path / "out", parse_workers=parse_workers)
    assert not (tmp_path / "out" / "r_combined.xlsx").exists()


@pytest.mark.parametrize("parse_workers", [1, 2])
@pytest.mark.parametrize("name", _ERRORS)
def test_part_not_read_keeps_the_workbook_and_raises_the_error(tmp_path: Path, name: str, parse_workers: int) -> None:
    change, message = _ERRORS[name]
    summary = run_dump(_report(tmp_path, change(_DECLARED)), tmp_path / "out", parse_workers=parse_workers)
    errors = [brick.error for brick in summary.bricks if brick.error]
    assert errors and message in errors[0]
    assert summary.bricks[0].status == "checks failed"
    # The problem opens the first sheet of the workbook.
    workbook = tmp_path / "out" / "r_combined.xlsx"
    about = list(load_workbook(workbook, read_only=True)["About"].iter_rows(values_only=True))
    assert about[0][0].startswith("CHECKS FAILED")
    assert any(row[0] == "Problem" and message in row[1] for row in about if len(row) > 1)


@pytest.mark.parametrize("parse_workers", [1, 2])
@pytest.mark.parametrize(
    "change",
    [
        lambda xml: xml,
        lambda xml: xml.replace("http://pa.cellebrite.com/report/2.0", "http://pa.cellebrite.com/report/3.0"),
        lambda xml: xml.replace("SMS seul", "SMS\x0bseul"),  # real reports hold invalid characters
    ],
    ids=["unchanged", "namespace version", "invalid character"],
)
def test_sound_report_raises_nothing(tmp_path: Path, change, parse_workers: int) -> None:
    summary = run_dump(_report(tmp_path, change(_DECLARED)), tmp_path / "out", parse_workers=parse_workers)
    assert summary.record_count == 43 and not summary.findings
    assert [brick.status for brick in summary.bricks] == ["ok"]


def test_report_declaring_itself_empty_is_not_an_error(tmp_path: Path) -> None:
    xml = '<?xml version="1.0"?><project xmlns="http://pa.cellebrite.com/report/2.0" ModelCount="0"><decodedData></decodedData><extraInfos></extraInfos></project>'
    assert run_dump(_report(tmp_path, xml), tmp_path / "out").record_count == 0


def test_missing_model_alone_is_not_an_error(tmp_path: Path) -> None:
    """A phone with no email or contact is normal: absence proves nothing."""
    start, end = _DECLARED.index('<modelType type="Email">'), _DECLARED.index('<modelType type="Location">')
    summary = run_dump(_report(tmp_path, _DECLARED[:start] + _DECLARED[end:]), tmp_path / "out")
    assert [brick.status for brick in summary.bricks] == ["ok"]


def test_models_filter_can_read_nothing(tmp_path: Path) -> None:
    summary = run_dump(_report(tmp_path, _DECLARED), tmp_path / "out", models={"Absent"}, parse_workers=2, bricks=set())
    assert summary.record_count == 0


def _csv_cells(folder: Path) -> dict[str, set[tuple[tuple[str, str], ...]]]:
    """Each CSV's rows as (column, value) pairs, minus where the record sat and the input's path."""
    skipped = {PARENT_FIELD_COLNAME, *TRACE_COLUMNS}
    cells = {}
    for path in folder.glob("*.csv"):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            cells[path.name] = {
                tuple(sorted((k, v) for k, v in row.items() if k not in skipped and v))
                for row in csv.DictReader(handle)
            }
    return cells


@pytest.mark.parametrize("parse_workers", [1, 2])
@pytest.mark.parametrize("name", _ABSORBED)
def test_structure_change_loses_nothing(tmp_path: Path, name: str, parse_workers: int) -> None:
    original = tmp_path / "before"
    changed = tmp_path / "after"
    original.mkdir()
    changed.mkdir()
    run_dump(_report(original, _DECLARED), original / "out", parse_workers=parse_workers)
    summary = run_dump(_report(changed, _ABSORBED[name](_DECLARED)), changed / "out", parse_workers=parse_workers)
    assert summary.record_count == 43 and not summary.findings
    assert [brick.status for brick in summary.bricks] == ["ok"]
    assert _csv_cells(changed / "out") == _csv_cells(original / "out")


def test_models_outside_decoded_data_are_read(tmp_path: Path) -> None:
    extra = '<model type="Note" id="n1"><field name="Title" type="String"><value type="String">x</value></field></model>'
    summary = run_dump(_report(tmp_path, _DECLARED.replace("<decodedData>", extra + "<decodedData>")), tmp_path / "out", bricks=set())
    assert summary.record_count == 44 and not summary.findings
    assert (tmp_path / "out" / "r_Note.csv").exists()


@pytest.mark.parametrize("parse_workers", [1, 2])
def test_models_outside_the_blocks_are_read(tmp_path: Path, parse_workers: int) -> None:
    """Parallel parsing works on <modelType> blocks: what lies outside them must not be lost."""
    extra = '<model type="Note" id="n1"><field name="Title" type="String"><value type="String">x</value></field></model>'
    xml = _DECLARED.replace("</decodedData>", extra + "</decodedData>")
    summary = run_dump(_report(tmp_path, xml), tmp_path / "out", parse_workers=parse_workers, bricks=set())
    assert summary.record_count == 44 and not summary.findings


def test_related_models_are_extracted_and_listed(tmp_path: Path) -> None:
    related = (
        '<RelatedModels><model type="Location" id="rel1">'
        '<field name="Name" type="String"><value type="String">relatedplace</value></field></model></RelatedModels>'
    )
    xml = _DECLARED.replace('<model type="Location" id="loc1">', '<model type="Location" id="loc1">' + related)
    summary = run_dump(_report(tmp_path, xml), tmp_path / "out")
    assert summary.record_count == 44 and not summary.findings and not summary.omissions
    with (tmp_path / "out" / "r_SMLocation.csv").open(encoding="utf-8-sig", newline="") as handle:
        row = next(r for r in csv.DictReader(handle) if r["Name"] == "relatedplace")
    assert row[PARENT_FIELD_COLNAME] == "RelatedModels"
    sheet = load_workbook(tmp_path / "out" / "r_combined.xlsx", read_only=True)["Locations"]
    assert any("relatedplace" in row for row in sheet.iter_rows(values_only=True))


def test_nested_model_reaches_the_sheet_at_any_depth(tmp_path: Path) -> None:
    """A message quoted three levels down is still a communication; nothing stays CSV-only."""
    quote = (
        '<modelField name="Quoted" type="InstantMessage"><model type="InstantMessage" id="deep1">'
        '<field name="Body" type="String"><value type="String">quoted text</value></field></model></modelField>'
    )
    xml = _DECLARED.replace('<model type="Party" id="p1">', '<model type="Party" id="p1">' + quote, 1)
    assert quote in xml
    summary = run_dump(_report(tmp_path, xml), tmp_path / "out")
    assert not summary.findings and [brick.status for brick in summary.bricks] == ["ok"]
    assert (tmp_path / "out" / "r_SSSMInstantMessage.csv").exists()
    sheet = load_workbook(tmp_path / "out" / "r_combined.xlsx", read_only=True)["Communications"]
    assert any("quoted text" in row for row in sheet.iter_rows(values_only=True))


def test_chat_without_message_is_listed(tmp_path: Path) -> None:
    empty = '<model type="Chat" id="chat9"><field name="Name" type="String"><value type="String">Silent group</value></field></model>'
    xml = _DECLARED.replace('<modelType type="Chat">', '<modelType type="Chat">' + empty)
    summary = run_dump(_report(tmp_path, xml), tmp_path / "out")
    assert not summary.findings
    rows = list(load_workbook(tmp_path / "out" / "r_combined.xlsx", read_only=True)["Communications"].iter_rows(values_only=True))
    assert any("Chat without message" in row and "Silent group" in row for row in rows)


def test_csv_no_sheet_reads_is_listed_on_about(tmp_path: Path) -> None:
    unknown = '<modelType type="Mystery"><model type="Mystery" id="q1"><field name="X" type="String"><value type="String">v</value></field></model></modelType>'
    summary = run_dump(_report(tmp_path, _DECLARED.replace("</decodedData>", unknown + "</decodedData>")), tmp_path / "out")
    assert summary.bricks[0].status == "ok"
    about = list(load_workbook(tmp_path / "out" / "r_combined.xlsx", read_only=True)["About"].iter_rows(values_only=True))
    assert any(row[0] == "In the raw CSVs only (on no sheet)" and "r_Mystery.csv" in row[1] for row in about if len(row) > 1)


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 64, 100_000])
def test_model_tag_counter(chunk: int) -> None:
    data = (
        b'<modelType type="X"><model id="1"/><![CDATA[ <model id="decoy"> ]]>'
        b'<modelField><model\n id="2"></model></modelField><![CDATA[ <model ]]><model>x</model></modelType>'
    )
    counter = ModelTagCounter()
    for start in range(0, len(data), chunk):
        counter.feed(data[start : start + chunk])
    assert counter.finish() == 3
