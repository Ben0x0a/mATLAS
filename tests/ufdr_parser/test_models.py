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
    assert party.fields["Identifier"] == "+34123"
