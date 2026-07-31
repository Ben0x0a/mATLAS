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
# and a Call (1:many Parties, inlined as indexed Parties.N.* columns), with a matching
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


def test_xml_multimodelfield_is_inlined_on_the_parent_row(tmp_path: Path) -> None:
    # FIX-21 CB3: multiModelField (1:many) children are the only place nested evidence is
    # captured for the pipeline (nothing re-reads the report), so they must surface here.
    source = _report(tmp_path)
    [record] = list(XmlReader().iter_records(source, "Call"))
    assert record.fields["Direction"] == "Outgoing"
    assert record.fields["source_table"] == "ZCALLRECORD"
    # The single Party surfaces under an indexed prefix on the Call's row.
    assert record.fields["Parties.0.Identifier"] == "+34"


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


# FIX-21 #1: verbatim capture must not lose repeated field names, and a model <field> named
# like an injected forensic attribute must not overwrite the captured attribute.
_CLOBBER_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<project xmlns="http://pa.cellebrite.com/report/2.0" name="t">
  <decodedData>
    <modelType type="Note">
      <model type="Note" id="note1" deleted_state="Intact">
        <field name="Body" type="String"><value type="String"><![CDATA[first]]></value></field>
        <field name="Body" type="String"><value type="String"><![CDATA[second]]></value></field>
        <field name="uuid" type="String"><value type="String"><![CDATA[not-the-real-id]]></value></field>
      </model>
    </modelType>
  </decodedData>
  <extraInfos/>
</project>
"""


def test_xml_duplicate_and_attribute_named_fields_are_kept_losslessly(tmp_path: Path) -> None:
    path = tmp_path / "clobber.xml"
    path.write_text(_CLOBBER_REPORT, encoding="utf-8")
    [record] = list(XmlReader().iter_records(path, "Note"))
    # Both values of the repeated <field name="Body"> survive (no clobber).
    assert record.fields["Body"] == "first"
    assert record.fields["Body#2"] == "second"
    # The injected forensic uuid keeps the real model id; the colliding field is disambiguated.
    assert record.fields["uuid"] == "note1"
    assert record.fields["uuid#2"] == "not-the-real-id"
    assert record.fields["deleted_state"] == "Intact"


def test_xml_multimodelfield_keeps_every_party(tmp_path: Path) -> None:
    report = _REPORT.replace(
        '<multiModelField name="Parties" type="Party">\n'
        '          <model type="Party" id="p1"><field name="Identifier" type="String">'
        '<value type="String"><![CDATA[+34]]></value></field></model>\n'
        '        </multiModelField>',
        '<multiModelField name="Parties" type="Party">\n'
        '          <model type="Party" id="p1"><field name="Identifier" type="String">'
        '<value type="String"><![CDATA[+34]]></value></field></model>\n'
        '          <model type="Party" id="p2"><field name="Identifier" type="String">'
        '<value type="String"><![CDATA[+49]]></value></field></model>\n'
        '        </multiModelField>',
    )
    path = tmp_path / "twoparties.xml"
    path.write_text(report, encoding="utf-8")
    [record] = list(XmlReader().iter_records(path, "Call"))
    assert record.fields["Parties.0.Identifier"] == "+34"
    assert record.fields["Parties.1.Identifier"] == "+49"


def test_ufdr_workdir_path_with_special_chars_reads(tmp_path: Path) -> None:
    # FIX-21 #3: a work-dir path with '#'/'?'/'%' must not break the read-only source URI.
    from model_atlas.ingest.readers.xml_specifics.cellebrite_xml_reports import (
        CellebriteDialect,
    )

    source = _report(tmp_path)
    work_dir = tmp_path / "case #1 ? 50%"
    work_dir.mkdir()
    dialect = CellebriteDialect()
    prep = dialect.prepare(lambda: source.open("rb"), work_dir)
    frame, _columns = dialect.read_model(prep, "Location")
    # The source join (which opens the file: URI in the '#'-bearing work dir) succeeded.
    assert frame.iloc[0]["source_table"] == "ZRTCLLOCATION"
