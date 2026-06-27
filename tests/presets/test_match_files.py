"""match_files: one file feeds one preset per distinct in-file unit (model type)."""
from __future__ import annotations

from pathlib import Path, PurePosixPath

import yaml

from model_atlas.presets.matcher import match_files, match_files_detailed
from model_atlas.presets.spec import preset_spec_from_yaml
from model_atlas.sources.container import FilesystemContainer, SourceFile
from tests.sources.test_xml_cellebrite import _REPORT

_NS = "http://pa.cellebrite.com/report/2.0"


def _xml_preset(model: str, *, id_suffix: str = "", longitude=None) -> object:
    position = {"latitude_wgs84": "column(Position.Latitude)"}
    if longitude is not None:
        position["longitude_wgs84"] = longitude
    raw = {
        "preset": {"id": f"cellebrite.{model.lower()}{id_suffix}", "name": f"Cellebrite {model}{id_suffix}",
                   "tier": "secondary", "version": 1},
        "input_selector": {"format": "xml", "name": "report.xml", "namespace": _NS, "model": model},
        "assertions": [{
            "position": position,
            "time": {"instant": "column(TimeStamp)"},
            "links": {"entity_position": "at", "entity_time": "observed_at",
                      "spatial_temporal": "instant"},
        }],
    }
    return preset_spec_from_yaml(yaml.safe_load(yaml.safe_dump(raw)), Path(f"{model}.yaml"))


def _report_file(tmp_path: Path) -> SourceFile:
    (tmp_path / "report.xml").write_text(_REPORT, encoding="utf-8")
    return SourceFile(containers=(FilesystemContainer(tmp_path),),
                      logical_path=PurePosixPath("report.xml"))


# Peek that reports a fixed present-column set (gate input), ignoring the selector.
def _peek(present):
    return lambda file, selector: set(present)


def test_one_report_matches_one_preset_per_model(tmp_path: Path) -> None:
    (tmp_path / "report.xml").write_text(_REPORT, encoding="utf-8")
    file = SourceFile(containers=(FilesystemContainer(tmp_path),),
                      logical_path=PurePosixPath("report.xml"))
    presets = [_xml_preset("Location"), _xml_preset("Call")]

    matches = match_files(file, presets, peek=None)

    assert {sel.model for _preset, sel in matches} == {"Location", "Call"}
    assert len(matches) == 2  # both model types matched the single report.xml


def test_namespace_mismatch_excludes_preset(tmp_path: Path) -> None:
    (tmp_path / "report.xml").write_text(_REPORT, encoding="utf-8")
    file = SourceFile(containers=(FilesystemContainer(tmp_path),),
                      logical_path=PurePosixPath("report.xml"))
    location = _xml_preset("Location")
    raw = {
        "preset": {"id": "axiom.location", "name": "AXIOM Loc", "tier": "secondary", "version": 1},
        "input_selector": {"format": "xml", "name": "report.xml",
                           "namespace": "urn:axiom", "model": "Location"},
        "assertions": location.assertions and [{
            "position": {"latitude_wgs84": "column(Position.Latitude)"},
            "time": {"instant": "column(TimeStamp)"},
            "links": {"entity_position": "at", "entity_time": "observed_at",
                      "spatial_temporal": "instant"},
        }],
    }
    axiom = preset_spec_from_yaml(yaml.safe_load(yaml.safe_dump(raw)), Path("axiom.yaml"))

    matches = match_files(file, [location, axiom], peek=None)
    # Only the Cellebrite-namespace preset matches; the AXIOM-namespace one is excluded.
    assert [p.meta.id for p, _ in matches] == ["cellebrite.location"]


def test_missing_required_column_disqualifies_loudly(tmp_path: Path) -> None:
    file = _report_file(tmp_path)
    bad = _xml_preset("Location", longitude="column(NoSuchColumn)")
    present = {"Position.Latitude", "TimeStamp"}  # NoSuchColumn absent
    matches, unsatisfied = match_files_detailed(file, [bad], peek=_peek(present))
    assert matches == []                                   # not silently mapped
    assert len(unsatisfied) == 1
    assert "NoSuchColumn" in unsatisfied[0][2]             # the missing required column


def test_optional_column_absence_does_not_disqualify(tmp_path: Path) -> None:
    file = _report_file(tmp_path)
    opt = _xml_preset("Location", longitude={"from": "column(NoSuchColumn)", "optional": True})
    present = {"Position.Latitude", "TimeStamp"}
    matches, unsatisfied = match_files_detailed(file, [opt], peek=_peek(present))
    assert len(matches) == 1 and unsatisfied == []         # optional absence tolerated


def test_alternative_preset_covers_unit(tmp_path: Path) -> None:
    file = _report_file(tmp_path)
    needs = _xml_preset("Location", id_suffix=".v2", longitude="column(NoSuchColumn)")
    plain = _xml_preset("Location", id_suffix=".v1")       # no NoSuchColumn dependency
    present = {"Position.Latitude", "TimeStamp"}
    matches, unsatisfied = match_files_detailed(file, [needs, plain], peek=_peek(present))
    # The qualifying preset covers the Location unit; the needy one is skipped, not "unsatisfied".
    assert [p.meta.id for p, _ in matches] == ["cellebrite.location.v1"]
    assert unsatisfied == []
