"""Tests for the level-logic flattening (ufdr_parser.models / reader)."""
from __future__ import annotations

import io

from ufdr_parser.const import PARENT_FIELD_COLNAME, level_file_prefix
from ufdr_parser.models import FlatRecord
from ufdr_parser.reader import iter_items
from ufdr_parser.source_lookup import SourceEntry
from tests.ufdr_parser.conftest import REPORT_XML


def _items() -> list:
    return list(iter_items(io.BytesIO(REPORT_XML.encode("utf-8"))))


def test_records_and_sources_emitted() -> None:
    items = _items()
    records = [i for i in items if isinstance(i, FlatRecord)]
    sources = [i for i in items if isinstance(i, SourceEntry)]
    by_type = {r.model_type for r in records}
    assert by_type == {"Location", "Coordinate", "Call", "Party"}
    assert {s.model_id for s in sources} == {"loc1", "call1"}


def test_top_level_and_nesting() -> None:
    records = [i for i in _items() if isinstance(i, FlatRecord)]
    location = next(r for r in records if r.model_type == "Location")
    assert location.level == 0
    assert location.model_id == "loc1"
    assert location.top_type == "Location"
    assert location.fields["Source"] == "Apple Maps"
    # An <empty/> field flattens to None.
    assert location.fields["Name"] is None

    coordinate = next(r for r in records if r.model_type == "Coordinate")
    assert coordinate.level == 1
    assert coordinate.model_id == "coord1"
    assert coordinate.parent_id == "loc1"        # main-uuid is the parent model id
    assert coordinate.top_type == "Location"      # relation anchor is the top model
    assert coordinate.fields["Latitude"] == "38.34"


def test_multimodelfield_child() -> None:
    records = [i for i in _items() if isinstance(i, FlatRecord)]
    party = next(r for r in records if r.model_type == "Party")
    assert party.level == 1
    assert party.parent_id == "call1"
    assert party.top_type == "Call"
    assert party.top_id == "call1"          # tree root id, used to inherit the source
    assert party.fields["Identifier"] == "+34123"


# FIX-18 #4: a field with several <value> children must keep them all, not just the first.
_MULTI_VALUE_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<project xmlns="http://pa.cellebrite.com/report/2.0" name="mv">
  <decodedData>
    <modelType type="Chat">
      <model type="Chat" id="chat1">
        <field name="Participants" type="String">
          <value type="String"><![CDATA[alice]]></value>
          <value type="String"><![CDATA[bob]]></value>
          <value type="String"><![CDATA[carol]]></value>
        </field>
      </model>
    </modelType>
  </decodedData>
  <extraInfos/>
</project>
"""


def test_multi_value_field_joins_all_values() -> None:
    items = list(iter_items(io.BytesIO(_MULTI_VALUE_REPORT.encode("utf-8"))))
    chat = next(i for i in items if isinstance(i, FlatRecord) and i.model_type == "Chat")
    assert chat.fields["Participants"] == "alice; bob; carol"


_NESTED_XML = """<?xml version="1.0" encoding="utf-8"?>
<project xmlns="http://pa.cellebrite.com/report/2.0"><decodedData>
<modelType type="Chat">
  <model type="Chat" id="c1" deleted_state="Intact" iscarved="False">
    <field name="Name" type="String"><value type="String"><![CDATA[Famille]]></value></field>
    <field name="Label" type="String"><value type="String">first</value></field>
    <field name="Label" type="String"><value type="String">second</value></field>
    <multiField name="Tags" type="String"><value type="String">a</value><value type="String">b</value></multiField>
    <field name="When" type="TimeStamp"><value type="TimeStamp" format="ISO" formattedTimestamp="1 Jan">2020-01-01T00:00:00+02:00</value></field>
    <jumptargets targetType="File"><targetid>t1</targetid><targetid>t2</targetid></jumptargets>
    <multiModelField name="Messages" type="InstantMessage">
      <model type="InstantMessage" id="m1">
        <field name="Body" type="String"><value type="String"><![CDATA[salut]]></value></field>
        <multiModelField name="Parties" type="Party">
          <model type="Party" id="p1">
            <field name="Identifier" type="String"><value type="String"><![CDATA[+41]]></value></field>
            <modelField name="Deeper" type="Note">
              <model type="Note" id="deep"><field name="X" type="String"><value type="String">x</value></field></model>
            </modelField>
          </model>
        </multiModelField>
      </model>
    </multiModelField>
    <RelatedModels><model type="Location" id="rel" /></RelatedModels>
    <somethingNew><model type="Odd" id="odd" /></somethingNew>
  </model>
</modelType>
</decodedData></project>
"""


def _nested() -> dict[str, FlatRecord]:
    records = (i for i in iter_items(io.BytesIO(_NESTED_XML.encode())) if isinstance(i, FlatRecord))
    return {record.model_id: record for record in records}


def test_every_model_becomes_a_record_wherever_it_sits() -> None:
    records = _nested()
    assert set(records) == {"c1", "m1", "p1", "deep", "rel", "odd"}
    deep = records["deep"]
    assert (deep.level, deep.parent_id, deep.top_id, deep.top_type) == (3, "p1", "c1", "Chat")
    assert deep.fields[PARENT_FIELD_COLNAME] == "Deeper"
    assert (records["rel"].level, records["rel"].fields[PARENT_FIELD_COLNAME]) == (1, "RelatedModels")
    assert (records["odd"].level, records["odd"].fields[PARENT_FIELD_COLNAME]) == (1, "somethingNew")
    assert level_file_prefix(deep.level) == "SSSM"


def test_every_attribute_and_child_becomes_a_column() -> None:
    fields = _nested()["c1"].fields
    assert (fields["deleted_state"], fields["iscarved"]) == ("Intact", "False")
    assert (fields["Label"], fields["Label#2"]) == ("first", "second")
    assert fields["Tags"] == "a; b"
    assert fields["When"] == "2020-01-01T00:00:00+02:00"
    assert (fields["When.format"], fields["When.formattedTimestamp"]) == ("ISO", "1 Jan")
    assert fields["When.offset"] == "+02:00"
    assert (fields["jumptargets"], fields["jumptargets.targetType"]) == ("t1; t2", "File")
    assert PARENT_FIELD_COLNAME not in fields or not fields[PARENT_FIELD_COLNAME]
