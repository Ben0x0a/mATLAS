"""Tests for the level-logic flattening (ufdr_parser.models / reader)."""
from __future__ import annotations

import io

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


# A top-level model nesting three levels deep (the grandchild's child is past the
# decomposition) plus a model hanging off a non-model container, which is not part of it.
_NESTED_XML = """<?xml version="1.0" encoding="utf-8"?>
<project xmlns="http://pa.cellebrite.com/report/2.0"><decodedData>
<modelType type="Chat">
  <model type="Chat" id="c1">
    <field name="Name" type="String"><value type="String"><![CDATA[Famille]]></value></field>
    <multiModelField name="Messages" type="InstantMessage">
      <model type="InstantMessage" id="m1">
        <field name="Body" type="String"><value type="String"><![CDATA[salut]]></value></field>
        <multiModelField name="Parties" type="Party">
          <model type="Party" id="p1">
            <field name="Identifier" type="String"><value type="String"><![CDATA[+41]]></value></field>
            <modelField name="Deeper" type="Note">
              <model type="Note" id="too-deep"><field name="X" type="String"><value type="String">x</value></field></model>
            </modelField>
          </model>
        </multiModelField>
      </model>
      <model type="InstantMessage" id="m2">
        <field name="Body" type="String"><value type="String"><![CDATA[ça va]]></value></field>
      </model>
    </multiModelField>
    <jumptargets><model type="Odd" id="outside-chain" /></jumptargets>
  </model>
  <model type="Chat" id="c2">
    <field name="Name" type="String"><value type="String"><![CDATA[Seul]]></value></field>
  </model>
</modelType>
</decodedData></project>
"""


def test_streaming_reader_matches_recursive_decomposition() -> None:
    """The reader emits each model at its own end tag; it must produce exactly the records
    the recursive reference decomposition (flatten_model over the whole tree) produces."""
    from lxml import etree

    from ufdr_parser.models import flatten_model

    streamed = [i for i in iter_items(io.BytesIO(_NESTED_XML.encode())) if isinstance(i, FlatRecord)]
    root = etree.fromstring(_NESTED_XML.encode())
    reference = [
        record
        for model_type in root.iter("{http://pa.cellebrite.com/report/2.0}modelType")
        for model in model_type
        for record in flatten_model(model)
    ]
    key = lambda r: (r.level, r.model_type, r.model_id)  # noqa: E731
    assert sorted(streamed, key=key) == sorted(reference, key=key)
    ids = {r.model_id for r in streamed}
    assert "too-deep" not in ids and "outside-chain" not in ids
    party = next(r for r in streamed if r.model_id == "p1")
    assert (party.level, party.parent_id, party.top_id, party.top_type) == (2, "m1", "c1", "Chat")
