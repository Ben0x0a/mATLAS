"""End-to-end tests for run_dump: CSV output, source join, filtering, archive input."""
from __future__ import annotations

import csv
import zipfile
from pathlib import Path

import pytest

from ufdr_parser.dump import run_dump


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _write(tmp_path: Path, name: str, xml: str) -> Path:
    path = tmp_path / name
    path.write_text(xml, encoding="utf-8")
    return path


def test_dump_writes_per_type_csvs(report_xml: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    summary = run_dump(report_xml, out)
    base = report_xml.stem
    names = set(summary.files)
    assert names == {
        f"{base}_Location.csv",
        f"{base}_SMCoordinate.csv",
        f"{base}_Call.csv",
        f"{base}_SMParty.csv",
    }
    assert summary.record_count == 4
    assert summary.source_count == 2


def test_location_row_columns_and_source_join(report_xml: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    run_dump(report_xml, out)
    rows = _read_csv(out / f"{report_xml.stem}_Location.csv")
    assert len(rows) == 1
    row = rows[0]
    assert row["uuid"] == "loc1"
    assert row["Source"] == "Apple Maps"
    assert row["Name"] == ""                       # <empty/> -> blank cell
    assert row["source_path"] == "files_full.zip/private/Cache.sqlite"
    assert row["source_table"] == "ZRTCLLOCATION"
    assert row["source_offset"] == "123"


def test_submodel_uuids(report_xml: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    run_dump(report_xml, out)
    rows = _read_csv(out / f"{report_xml.stem}_SMCoordinate.csv")
    assert rows[0]["sub-uuid"] == "coord1"
    assert rows[0]["main-uuid"] == "loc1"
    assert rows[0]["Latitude"] == "38.34"


def test_call_source_join(report_xml: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    run_dump(report_xml, out)
    rows = _read_csv(out / f"{report_xml.stem}_Call.csv")
    assert rows[0]["source_table"] == "ZCALLRECORD"
    assert rows[0]["source_offset"] == "1159850"


def test_models_filter_keeps_children_drops_others(report_xml: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    summary = run_dump(report_xml, out, models={"Location"})
    base = report_xml.stem
    assert set(summary.files) == {f"{base}_Location.csv", f"{base}_SMCoordinate.csv"}


def test_relations_tracked(report_xml: Path, tmp_path: Path) -> None:
    summary = run_dump(report_xml, tmp_path / "out")
    assert ("Coordinate", "Location") in summary.relations
    assert ("Party", "Call") in summary.relations


def test_dump_from_ufdr_archive(report_ufdr: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    summary = run_dump(report_ufdr, out)
    assert summary.record_count == 4
    rows = _read_csv(out / f"{report_ufdr.stem}_Call.csv")
    assert rows[0]["uuid"] == "call1"


def test_submodel_inherits_parent_source(report_xml: Path, tmp_path: Path) -> None:
    # FIX-18 #3: extraInfos is keyed by top-level ids, so nested rows must inherit the
    # top-level model's source rather than emitting blank source columns.
    out = tmp_path / "out"
    run_dump(report_xml, out)
    coord = _read_csv(out / f"{report_xml.stem}_SMCoordinate.csv")[0]
    assert coord["source_table"] == "ZRTCLLOCATION"      # inherited from Location loc1
    party = _read_csv(out / f"{report_xml.stem}_SMParty.csv")[0]
    assert party["source_table"] == "ZCALLRECORD"         # inherited from Call call1


# FIX-18 #1: a top-level 'SMS' (prefix "") and a level-1 'S' (prefix "SM") both mapped to
# <base>_SMS.csv; the second open() truncated the first. They must now be distinct files.
_COLLIDING_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<project xmlns="http://pa.cellebrite.com/report/2.0" name="c">
  <decodedData>
    <modelType type="SMS">
      <model type="SMS" id="sms1"><field name="Body" type="String"><value type="String"><![CDATA[hi]]></value></field></model>
    </modelType>
    <modelType type="Wrapper">
      <model type="Wrapper" id="w1">
        <modelField name="Child" type="S">
          <model type="S" id="s1"><field name="Tag" type="String"><value type="String"><![CDATA[x]]></value></field></model>
        </modelField>
      </model>
    </modelType>
  </decodedData>
  <extraInfos/>
</project>
"""


def test_filename_collision_writes_two_reconciling_files(tmp_path: Path) -> None:
    report = _write(tmp_path, "collide.xml", _COLLIDING_REPORT)
    out = tmp_path / "out"
    summary = run_dump(report, out)
    base = report.stem
    # The top-level SMS keeps its legacy name; the colliding level-1 'S' is disambiguated.
    assert f"{base}_SMS.csv" in summary.files
    assert f"{base}_SMS__L1.csv" in summary.files
    # Both files exist with one row each — counts reconcile (no truncation).
    assert summary.files[f"{base}_SMS.csv"] == 1
    assert summary.files[f"{base}_SMS__L1.csv"] == 1
    assert _read_csv(out / f"{base}_SMS.csv")[0]["Body"] == "hi"
    assert _read_csv(out / f"{base}_SMS__L1.csv")[0]["Tag"] == "x"


# FIX-18 #2: a model <field> named like an injected identity/source column must not shadow it.
_SHADOW_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<project xmlns="http://pa.cellebrite.com/report/2.0" name="s">
  <decodedData>
    <modelType type="Doc">
      <model type="Doc" id="doc1">
        <field name="uuid" type="String"><value type="String"><![CDATA[field-uuid]]></value></field>
        <field name="source_path" type="String"><value type="String"><![CDATA[field-path]]></value></field>
      </model>
    </modelType>
  </decodedData>
  <extraInfos>
    <extraInfo type="model" id="doc1"><sourceInfo><nodeInfos><nodeInfo name="d.db" path="real/source.db" tableName="ZDOC" offset="5" size="6"/></nodeInfos></sourceInfo></extraInfo>
  </extraInfos>
</project>
"""


def test_injected_columns_survive_field_name_collision(tmp_path: Path) -> None:
    report = _write(tmp_path, "shadow.xml", _SHADOW_REPORT)
    out = tmp_path / "out"
    run_dump(report, out)
    row = _read_csv(out / f"{report.stem}_Doc.csv")[0]
    # Injected identity + source keep their real values under the canonical column names.
    assert row["uuid"] == "doc1"
    assert row["source_path"] == "real/source.db"
    # The colliding model fields survive losslessly under disambiguated columns.
    assert row["uuid#2"] == "field-uuid"
    assert row["source_path#2"] == "field-path"


# FIX-18 #5: a literal <decodedData inside a taggedFiles CDATA must NOT anchor the parse.
_FAKE_MARKER_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<project xmlns="http://pa.cellebrite.com/report/2.0" name="f">
  <taggedFiles>
    <file id="f1" name="a"><meta><![CDATA[junk <decodedData spoof <modelType type="Fake"]]></meta></file>
  </taggedFiles>
  <decodedData>
    <modelType type="Location">
      <model type="Location" id="loc1"><field name="Source" type="String"><value type="String"><![CDATA[Real]]></value></field></model>
    </modelType>
  </decodedData>
  <extraInfos/>
</project>
"""


def test_cdata_marker_in_taggedfiles_does_not_misanchor(tmp_path: Path) -> None:
    report = _write(tmp_path, "fake_marker.xml", _FAKE_MARKER_REPORT)
    out = tmp_path / "out"
    summary = run_dump(report, out)
    base = report.stem
    # Only the REAL Location modelType is indexed (the spoof inside CDATA is ignored).
    assert set(summary.files) == {f"{base}_Location.csv"}
    assert _read_csv(out / f"{base}_Location.csv")[0]["Source"] == "Real"


def test_report_entry_found_in_case_subfolder(tmp_path: Path) -> None:
    # FIX-18 #5: a report stored under a case variant in a subfolder is still found.
    from tests.ufdr_parser.conftest import REPORT_XML

    archive = tmp_path / "nested.ufdr"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("Case1/REPORT.XML", REPORT_XML)
    summary = run_dump(archive, tmp_path / "out")
    assert summary.record_count == 4


def test_missing_report_raises_valueerror_serial_and_parallel(tmp_path: Path) -> None:
    # FIX-18 #5: an archive with no report.xml must raise a clear ValueError on both the
    # serial path (open_report) and the parallel path (report_compression), never a KeyError.
    bad = tmp_path / "no_report.ufdr"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("something_else.txt", "not a report")
    with pytest.raises(ValueError):
        run_dump(bad, tmp_path / "s")
    with pytest.raises(ValueError):
        run_dump(bad, tmp_path / "p", parse_workers=2)
