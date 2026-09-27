"""Tests for record provenance in the dump: source resolution, report traceability, offsets.

Defines:    tests for by-reference ``<nodeInfo id=..>`` source resolution (serial, parallel and
            the index_source_stream path the model_atlas Cellebrite dialect uses), the
            ``report_file`` / ``report_entry`` traceability columns, the explicit TimeStamp
            ``.offset`` column, input_kind, and the empty-extraInfos warning.
Used by:    pytest.
Depends on: ufdr_parser (dump, archive, source_lookup, reader), conftest fixtures.
"""
from __future__ import annotations

import csv
import io
import logging
import sqlite3
import zipfile
from pathlib import Path

import pytest

from ufdr_parser.archive import INPUT_UFDR, INPUT_XML, input_kind
from ufdr_parser.dump import run_dump
from ufdr_parser.reader import extra_infos_stream
from ufdr_parser.source_lookup import index_source_stream, resolve_from_conn

# A recent-PA-shaped report: model sources point at file NODES by id. loc1 resolves in one
# hop; call1 in two (a derived node -> the file node); the node entries are deliberately
# split around the model entries so resolution cannot depend on document order.
BY_REFERENCE_XML = """<?xml version="1.0" encoding="utf-8"?>
<project xmlns="http://pa.cellebrite.com/report/2.0" name="byref">
  <decodedData>
    <modelType type="Location">
      <model type="Location" id="loc1">
        <field name="TimeStamp" type="TimeStamp"><value type="TimeStamp" format="TimeStampKnown">2025-04-16T03:49:26.902+00:00</value></field>
        <field name="EndTime" type="TimeStamp"><value type="TimeStamp" format="DateTimeOnly">2028-12-24T00:00:00.000</value></field>
        <field name="Source" type="String"><value type="String"><![CDATA[Native]]></value></field>
      </model>
    </modelType>
    <modelType type="Call">
      <model type="Call" id="call1">
        <field name="Direction" type="String"><value type="String"><![CDATA[Outgoing]]></value></field>
      </model>
    </modelType>
  </decodedData>
  <extraInfos>
    <extraInfo type="node" id="n-cache"><sourceInfo><nodeInfos><nodeInfo name="Cache.sqlite-wal" path="FFS.zip/root/Cache.sqlite-wal" size="2575032" tableName="" offset="0" /></nodeInfos></sourceInfo></extraInfo>
    <extraInfo type="model" id="loc1"><sourceInfo><nodeInfos><nodeInfo id="n-cache" tableName="ZRTCLLOCATIONMO" offset="1697088" /></nodeInfos></sourceInfo></extraInfo>
    <extraInfo type="model" id="call1"><sourceInfo><nodeInfos><nodeInfo id="n-derived" tableName="model_TSInteraction" offset="3152504" /></nodeInfos></sourceInfo></extraInfo>
    <extraInfo type="node" id="n-derived"><sourceInfo><nodeInfos><nodeInfo id="n-signal" tableName="" offset="0" /></nodeInfos></sourceInfo></extraInfo>
    <extraInfo type="node" id="n-signal"><sourceInfo><nodeInfos><nodeInfo name="signal.sqlite" path="FFS.zip/root/grdb/signal.sqlite" size="3260416" tableName="" offset="0" /></nodeInfos></sourceInfo></extraInfo>
  </extraInfos>
</project>
"""


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture
def byref_xml(tmp_path: Path) -> Path:
    path = tmp_path / "byref_Report.xml"
    path.write_text(BY_REFERENCE_XML, encoding="utf-8")
    return path


@pytest.mark.parametrize("parse_workers", [1, 2])
def test_by_reference_source_resolves_through_node_chain(
    byref_xml: Path, tmp_path: Path, parse_workers: int
) -> None:
    out = tmp_path / "out"
    summary = run_dump(byref_xml, out, parse_workers=parse_workers)
    # Node entries only serve resolution; they are not model sources.
    assert summary.source_count == 2

    loc = _read_csv(out / "byref_Report_Location.csv")[0]
    assert loc["source_path"] == "FFS.zip/root/Cache.sqlite-wal"
    assert loc["source_name"] == "Cache.sqlite-wal"
    assert loc["source_size"] == "2575032"
    # Table/offset locate the record INSIDE the file, so they stay the model's own.
    assert loc["source_table"] == "ZRTCLLOCATIONMO"
    assert loc["source_offset"] == "1697088"

    call = _read_csv(out / "byref_Report_Call.csv")[0]
    assert call["source_path"] == "FFS.zip/root/grdb/signal.sqlite"
    assert call["source_table"] == "model_TSInteraction"
    assert call["source_offset"] == "3152504"


def test_index_source_stream_resolves_references() -> None:
    # The model_atlas Cellebrite dialect builds its map through index_source_stream.
    start = BY_REFERENCE_XML.index("<extraInfos>")
    stream = io.BytesIO(BY_REFERENCE_XML[start:].encode())
    conn = sqlite3.connect(":memory:")
    assert index_source_stream(extra_infos_stream(stream), conn) == 2
    entry = resolve_from_conn(conn, "loc1")
    assert entry is not None and entry.path == "FFS.zip/root/Cache.sqlite-wal"


def test_self_referencing_node_terminates_blank(tmp_path: Path) -> None:
    xml = BY_REFERENCE_XML.replace(
        '<nodeInfo name="Cache.sqlite-wal" path="FFS.zip/root/Cache.sqlite-wal" size="2575032"',
        '<nodeInfo id="n-cache"',
    )
    path = tmp_path / "cycle.xml"
    path.write_text(xml, encoding="utf-8")
    run_dump(path, tmp_path / "out")
    loc = _read_csv(tmp_path / "out" / "cycle_Location.csv")[0]
    assert loc["source_path"] == ""
    assert loc["source_table"] == "ZRTCLLOCATIONMO"


def test_report_trace_columns_bare_xml(byref_xml: Path, tmp_path: Path) -> None:
    run_dump(byref_xml, tmp_path / "out")
    header = next(csv.reader((tmp_path / "out" / "byref_Report_Location.csv").open(encoding="utf-8-sig")))
    assert header[-2:] == ["report_file", "report_entry"]
    row = _read_csv(tmp_path / "out" / "byref_Report_Location.csv")[0]
    assert row["report_file"] == str(byref_xml.resolve())
    assert row["report_entry"] == ""


@pytest.mark.parametrize("parse_workers", [1, 2])
def test_report_trace_columns_ufdr(report_ufdr: Path, tmp_path: Path, parse_workers: int) -> None:
    out = tmp_path / "out"
    run_dump(report_ufdr, out, parse_workers=parse_workers)
    for name in (f"{report_ufdr.stem}_Location.csv", f"{report_ufdr.stem}_SMCoordinate.csv"):
        row = _read_csv(out / name)[0]
        assert row["report_file"] == str(report_ufdr.resolve())
        assert row["report_entry"] == "report.xml"


def test_timestamp_offset_is_explicit(byref_xml: Path, tmp_path: Path) -> None:
    run_dump(byref_xml, tmp_path / "out")
    row = _read_csv(tmp_path / "out" / "byref_Report_Location.csv")[0]
    assert row["TimeStamp"] == "2025-04-16T03:49:26.902+00:00"
    assert row["TimeStamp.offset"] == "+00:00"
    # DateTimeOnly carries no zone: stated as such, never guessed.
    assert row["EndTime.offset"] == "unstated"
    assert "Source.offset" not in row


def test_input_kind(byref_xml: Path, report_ufdr: Path, tmp_path: Path) -> None:
    assert input_kind(byref_xml) == INPUT_XML
    assert input_kind(report_ufdr) == INPUT_UFDR
    bom = tmp_path / "bom.dat"
    bom.write_bytes(b"\xef\xbb\xbf\n  <project/>")
    assert input_kind(bom) == INPUT_XML
    junk = tmp_path / "junk.xml"
    junk.write_bytes(b"not xml")
    assert input_kind(junk) is None


def test_empty_extra_infos_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    start = BY_REFERENCE_XML.index("<extraInfos>")
    end = BY_REFERENCE_XML.index("</extraInfos>") + len("</extraInfos>")
    path = tmp_path / "nosrc.xml"
    path.write_text(
        BY_REFERENCE_XML[:start] + "<extraInfos>\n</extraInfos>" + BY_REFERENCE_XML[end:],
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="ufdr_parser.dump"):
        summary = run_dump(path, tmp_path / "out")
    assert summary.source_count == 0
    assert any("no model source entries" in r.getMessage() for r in caplog.records)


def test_dump_of_zip_uses_original_path_not_temp_copy(tmp_path: Path) -> None:
    # A DEFLATE archive is extracted to a temp copy for parallel seeking; the trace must still
    # name the archive the operator supplied.
    path = tmp_path / "deflated.ufdr"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("report.xml", BY_REFERENCE_XML)
    run_dump(path, tmp_path / "out", parse_workers=2)
    row = _read_csv(tmp_path / "out" / "deflated_Location.csv")[0]
    assert row["report_file"] == str(path.resolve())
    assert row["source_path"] == "FFS.zip/root/Cache.sqlite-wal"
