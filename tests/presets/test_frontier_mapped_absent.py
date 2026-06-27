"""frontier_report.mapped_absent must detect a mapped column absent from the source.

Regression: it was computed as ``mapped - present`` where ``mapped`` only ever held present
columns, so it was always empty and a preset typo / schema drift slipped through silently.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from model_atlas.presets.spec import preset_spec_from_yaml
from model_atlas.reporting import frontier_report


def _preset(longitude_ref: str) -> object:
    raw = {
        "preset": {"id": "t.x", "name": "T", "tier": "secondary", "version": 1},
        "input_selector": {"format": "csv", "name": "x.csv"},
        "assertions": [{
            "position": {"latitude_wgs84": "column(Lat)", "longitude_wgs84": longitude_ref},
            "time": {"instant": "column(Ts)"},
            "links": {"entity_position": "at", "entity_time": "observed_at",
                      "spatial_temporal": "instant"},
        }],
    }
    return preset_spec_from_yaml(yaml.safe_load(yaml.safe_dump(raw)), Path("t.yaml"))


def test_mapped_absent_flags_missing_column() -> None:
    present = ["Lat", "Lon", "Ts"]
    assert frontier_report(_preset("column(Nope)"), present)["mapped_absent"] == ["Nope"]


def test_mapped_absent_empty_when_all_present() -> None:
    present = ["Lat", "Lon", "Ts"]
    assert frontier_report(_preset("column(Lon)"), present)["mapped_absent"] == []
