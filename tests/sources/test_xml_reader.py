"""Tests for the XmlReader FormatReader: read-in-place from a loose .xml and a .ufdr zip."""
from __future__ import annotations

import zipfile
from pathlib import Path, PurePosixPath

import pytest

from model_atlas.sources.container import FilesystemContainer, SourceFile, ZipContainer
from model_atlas.sources.readers.base import get_reader
from tests.sources.test_xml_cellebrite import _REPORT  # reuse the minimal report


def _fs_source(tmp_path: Path) -> SourceFile:
    (tmp_path / "report.xml").write_text(_REPORT, encoding="utf-8")
    container = FilesystemContainer(tmp_path)
    return SourceFile(containers=(container,), logical_path=PurePosixPath("report.xml"))


def _ufdr_source(tmp_path: Path) -> SourceFile:
    ufdr = tmp_path / "case.ufdr"
    with zipfile.ZipFile(ufdr, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("report.xml", _REPORT)
    container = ZipContainer(path=ufdr)
    return SourceFile(containers=(container,), logical_path=PurePosixPath("report.xml"))


@pytest.mark.parametrize("make_source", [_fs_source, _ufdr_source])
def test_xml_reader_reads_model_in_place(make_source, tmp_path: Path) -> None:
    file = make_source(tmp_path)
    reader = get_reader("xml")

    assert reader.list_subtables(file) == ["Location", "Call"]

    result = reader.read(file, {"namespace": None, "model": "Location"})
    assert result.metadata["format"] == "xml"
    assert result.metadata["model"] == "Location"
    assert result.metadata["source_fingerprint"]  # content hash recorded (read in place)
    assert result.metadata["integrity"]["mode"] == "in_place"

    row = result.dataframe.iloc[0].to_dict()
    assert row["Position.Latitude"] == "38.34"
    assert row["Address.City"] == "Alicante"
    assert row["source_table"] == "ZRTCLLOCATION"
    assert "Position.Latitude" in result.source_columns
    assert "source_path" in result.source_columns


def test_xml_reader_honours_tier(tmp_path: Path) -> None:
    file = _fs_source(tmp_path)
    reader = get_reader("xml")
    in_place = reader.read(file, {"model": "Location", "_tier": "secondary"})
    staged = reader.read(file, {"model": "Location", "_tier": "primary"})
    assert in_place.metadata["integrity"]["mode"] == "in_place"   # report read in place
    assert staged.metadata["integrity"]["mode"] == "full"          # primary -> staged copy
    # Same data regardless of how the report was acquired.
    assert in_place.dataframe.equals(staged.dataframe)


def test_xml_reader_peek_and_cache(tmp_path: Path) -> None:
    file = _fs_source(tmp_path)
    reader = get_reader("xml")
    # peek_columns for the Call model; then a full read of a different model reuses the prep.
    cols = reader.peek_columns(file, _Selector("Call"))
    assert "Direction" in cols
    loc = reader.read(file, {"model": "Location"})
    call = reader.read(file, {"model": "Call"})
    assert call.metadata["source_fingerprint"] == loc.metadata["source_fingerprint"]


class _Selector:
    """Minimal stand-in for InputSelector.peek (only `.model` is read)."""

    def __init__(self, model: str) -> None:
        self.model = model
