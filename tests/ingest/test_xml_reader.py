"""Tests for the ingest XmlReader + the Cellebrite dialect it dispatches to.

Ported from tests/sources/test_xml_reader.py and tests/sources/test_xml_cellebrite.py,
adapted to the plain-Path ``source`` argument (no container/staging brick exists yet)
and the ``Reader`` protocol's ``subunits``/``iter_records``/``peek_fields`` names.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("lxml")
from model_atlas.core.vocabularies import RecordState
from model_atlas.ingest.readers.xml_reader import XmlReader, get_dialect, sniff_namespace
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


def _report(tmp_path: Path) -> Path:
    path = tmp_path / "report.xml"
    path.write_text(_REPORT, encoding="utf-8")
    return path


def test_sniff_namespace_selects_cellebrite() -> None:
    assert sniff_namespace(_REPORT.encode("utf-8")[:512]) == NS
    assert get_dialect("urn:unknown") is None


def test_sniff_namespace_takes_the_root_not_a_child() -> None:
    # FIX-08 #6: a root WITHOUT a namespace must not be mis-typed from a namespaced child.
    doc = b'<?xml version="1.0"?><root><child xmlns="urn:child"/></root>'
    assert sniff_namespace(doc) is None


def test_large_leading_comment_does_not_hide_the_root_namespace(tmp_path: Path) -> None:
    # FIX-08 #6: the root's xmlns sits behind a >8 KB comment. A fixed 8 KB head would miss it
    # and drop the whole report; the reader grows its window until the root tag is complete.
    padding = "y" * 20000
    big = _REPORT.replace(
        '<project xmlns="http://pa.cellebrite.com/report/2.0" name="t">',
        f'<!-- {padding} -->\n<project xmlns="http://pa.cellebrite.com/report/2.0" name="t">',
        1,
    )
    path = tmp_path / "big.xml"
    path.write_text(big, encoding="utf-8")
    # A fixed 8 KB slice cannot see past the comment...
    assert sniff_namespace(big.encode("utf-8")[:8192]) is None
    # ...but the reader grows the window, dispatches to Cellebrite, and reads the models.
    assert XmlReader().subunits(path) == ["Location", "Call"]


def test_xml_reader_subunits_and_records_in_document_order(tmp_path: Path) -> None:
    source = _report(tmp_path)
    reader = XmlReader()

    assert reader.subunits(source) == ["Location", "Call"]

    [record] = list(reader.iter_records(source, "Location"))
    assert record.line_no == 1
    assert record.recovery_state == RecordState.LIVE
    assert record.fields["Position.Latitude"] == "38.34"
    assert record.fields["Address.City"] == "Alicante"
    assert record.fields["Name"] is None                    # <empty/> -> None
    assert record.fields["source_table"] == "ZRTCLLOCATION"


def test_xml_multimodelfield_is_skipped(tmp_path: Path) -> None:
    source = _report(tmp_path)
    [record] = list(XmlReader().iter_records(source, "Call"))
    assert record.fields["Direction"] == "Outgoing"
    assert record.fields["source_table"] == "ZCALLRECORD"
    assert not any(k.startswith("Parties") or k == "Identifier" for k in record.fields)


def test_xml_peek_fields_and_cache_reused_across_models(tmp_path: Path) -> None:
    source = _report(tmp_path)
    reader = XmlReader()
    cols = reader.peek_fields(source, "Call")
    assert "Direction" in cols
    assert reader.peek_fields(source, None) is None
    # A full read of a different model reuses the same per-report prep/cache.
    [loc] = list(reader.iter_records(source, "Location"))
    assert loc.fields["Source"] == "Apple Maps"


def test_xml_reread_yields_identical_line_no_sequence(tmp_path: Path) -> None:
    source = _report(tmp_path)
    first = [r.line_no for r in XmlReader().iter_records(source, "Location")]
    second = [r.line_no for r in XmlReader().iter_records(source, "Location")]
    assert first == second


def test_xml_requires_a_model_type(tmp_path: Path) -> None:
    source = _report(tmp_path)
    with pytest.raises(ValueError):
        next(XmlReader().iter_records(source, None))
