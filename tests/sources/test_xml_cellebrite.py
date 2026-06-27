"""Tests for the Cellebrite XML dialect: namespace sniff + inline-flattened DataFrames."""
from __future__ import annotations

from pathlib import Path

import pytest

import model_atlas.sources.readers.xml_specifics  # noqa: F401 - registers dialects
from model_atlas.sources.readers.xml_reader import get_dialect, sniff_namespace
from ufdr_parser.const import NS

# A minimal Cellebrite report: a Location (1:1 Position->Coordinate, Address->StreetAddress)
# and a Call (1:many Parties, which must be skipped by the inline flatten), with a matching
# extraInfos id->source block.
_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<project xmlns="http://pa.cellebrite.com/report/2.0" name="t">
  <taggedFiles><file id="f1" path="/a.jpg" name="a.jpg" source_index="2"><accessInfo/></file></taggedFiles>
  <decodedData>
    <modelType type="Location">
      <model type="Location" id="loc1">
        <field name="Source" type="String"><value type="String"><![CDATA[Apple Maps]]></value></field>
        <modelField name="Position" type="Coordinate">
          <model type="Coordinate" id="c1">
            <field name="Latitude" type="Double"><value type="Double"><![CDATA[38.34]]></value></field>
            <field name="Longitude" type="Double"><value type="Double"><![CDATA[-0.47]]></value></field>
          </model>
        </modelField>
        <modelField name="Address" type="StreetAddress">
          <model type="StreetAddress" id="a1">
            <field name="City" type="String"><value type="String"><![CDATA[Alicante]]></value></field>
          </model>
        </modelField>
        <field name="Name" type="String"><empty/></field>
      </model>
    </modelType>
    <modelType type="Call">
      <model type="Call" id="call1">
        <field name="Direction" type="ModelDirections"><value type="ModelDirections"><![CDATA[Outgoing]]></value></field>
        <multiModelField name="Parties" type="Party">
          <model type="Party" id="p1"><field name="Identifier" type="String"><value type="String"><![CDATA[+34]]></value></field></model>
        </multiModelField>
      </model>
    </modelType>
  </decodedData>
  <extraInfos>
    <extraInfo type="model" id="loc1"><sourceInfo><nodeInfos><nodeInfo name="Cache.sqlite" path="zip/Cache.sqlite" tableName="ZRTCLLOCATION" offset="123" size="9"/></nodeInfos></sourceInfo></extraInfo>
    <extraInfo type="model" id="call1"><sourceInfo><nodeInfos><nodeInfo name="CallHistory.storedata" path="zip/CallHistory.storedata" tableName="ZCALLRECORD" offset="7" size="9"/></nodeInfos></sourceInfo></extraInfo>
  </extraInfos>
</project>
"""


@pytest.fixture
def report(tmp_path: Path) -> Path:
    path = tmp_path / "report.xml"
    path.write_text(_REPORT, encoding="utf-8")
    return path


def test_sniff_namespace_selects_cellebrite() -> None:
    assert sniff_namespace(_REPORT.encode("utf-8")[:512]) == NS
    assert get_dialect(NS) is not None
    assert get_dialect("urn:unknown") is None


def test_model_types(report: Path, tmp_path: Path) -> None:
    dialect = get_dialect(NS)
    prep = dialect.prepare(lambda: report.open('rb'), tmp_path)
    assert dialect.model_types(prep) == ["Location", "Call"]


def test_location_inline_flatten_and_source(report: Path, tmp_path: Path) -> None:
    dialect = get_dialect(NS)
    prep = dialect.prepare(lambda: report.open('rb'), tmp_path)
    df, source_columns = dialect.read_model(prep, "Location")

    assert len(df) == 1
    row = df.iloc[0].to_dict()
    # 1:1 modelField children inlined as prefixed columns.
    assert row["Position.Latitude"] == "38.34"
    assert row["Position.Longitude"] == "-0.47"
    assert row["Address.City"] == "Alicante"
    assert row["Source"] == "Apple Maps"
    assert row["Name"] is None                       # <empty/> -> None
    # source provenance joined from extraInfos.
    assert row["source_table"] == "ZRTCLLOCATION"
    assert row["source_path"] == "zip/Cache.sqlite"
    assert "source_path" in source_columns           # mappable (raw_source_path) + passthrough
    assert "Position.Latitude" in source_columns


def test_multimodelfield_is_skipped(report: Path, tmp_path: Path) -> None:
    dialect = get_dialect(NS)
    prep = dialect.prepare(lambda: report.open('rb'), tmp_path)
    df, _ = dialect.read_model(prep, "Call")
    assert df.iloc[0]["Direction"] == "Outgoing"
    assert df.iloc[0]["source_table"] == "ZCALLRECORD"
    # The 1:many Parties is deferred (not inlined), so no Party columns appear.
    assert not any(col.startswith("Parties") or col == "Identifier" for col in df.columns)
