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
