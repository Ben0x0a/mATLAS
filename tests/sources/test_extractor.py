"""Unit tests for the Extractor layer: SingleSourceExtractor builds ExtractedData with
the agreed field meanings + recovery enrichment, rejects multi-role input, and the
deferred ScriptExtractor raises."""
from __future__ import annotations

import sqlite3
from pathlib import Path, PurePosixPath

import pytest

from model_atlas.presets.spec import InputSelector
from model_atlas.sources.container import FilesystemContainer, SourceFile
from model_atlas.sources.extractor import ScriptExtractor, SingleSourceExtractor


def _db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER, lat REAL, lon REAL)")
    conn.execute("INSERT INTO t VALUES (1, 1.0, 2.0)")
    conn.commit()
    conn.close()
    return path


def test_single_source_extractor_builds_extracted_data(tmp_path: Path) -> None:
    _db(tmp_path / "x.sqlite")
    file = SourceFile(containers=(FilesystemContainer(tmp_path),),
                      logical_path=PurePosixPath("x.sqlite"))
    selector = InputSelector(format="sqlite", role="source", path="/x.sqlite", table="t")

    extracted = SingleSourceExtractor().extract({"source": (file, selector)}, preset=None)

    assert extracted.source_original_path == "x.sqlite"     # inner logical path (raw_source_path)
    assert extracted.source_file == "x.sqlite::table=t"
    assert extracted.source_fingerprint is not None
    assert extracted.origin is file
    # recovery_state is carried on enrichments, row-aligned, NOT in the dataframe.
    assert "recovery_state" in extracted.enrichments
    assert len(extracted.enrichments["recovery_state"]) == len(extracted.dataframe)
    assert "recovery_state" not in extracted.dataframe.columns


def test_single_source_extractor_rejects_multi_role() -> None:
    with pytest.raises(ValueError):
        SingleSourceExtractor().extract({"a": (None, None), "b": (None, None)}, preset=None)


def test_script_extractor_is_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        ScriptExtractor().extract({}, None)
