"""Unit tests for discover(): the folder / archive / single-file entrypoints, sidecar
skipping, error handling, and xlsx-is-a-file classification."""
from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook

from model_atlas.sources.discover import discover


def _db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()
    conn.close()
    return path


def test_discover_single_sqlite_file(tmp_path: Path) -> None:
    db = _db(tmp_path / "x.sqlite")
    files = discover(db)
    assert [f.name for f in files] == ["x.sqlite"]


def test_discover_folder_yields_loose_files(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    (tmp_path / "g.csv").write_text("x\n1\n", encoding="utf-8")
    files = discover(tmp_path)
    assert sorted(f.name for f in files) == ["f.csv", "g.csv"]


def test_discover_skips_sqlite_sidecars(tmp_path: Path) -> None:
    (tmp_path / "x.sqlite").write_bytes(b"SQLite format 3\x00")
    (tmp_path / "x.sqlite-wal").write_bytes(b"wal")
    (tmp_path / "x.sqlite-shm").write_bytes(b"shm")
    (tmp_path / "x.sqlite-journal").write_bytes(b"journal")
    files = discover(tmp_path)
    assert [f.name for f in files] == ["x.sqlite"]          # siblings never yielded


def test_discover_archive_yields_inner_logical_paths(tmp_path: Path) -> None:
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("filesystem1/private/x.sqlite", b"SQLite format 3\x00")
    files = discover(archive)
    assert [str(f.logical_path) for f in files] == ["filesystem1/private/x.sqlite"]
    assert files[0].container_chain == ["a.zip"]


def test_discover_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        discover(tmp_path / "does_not_exist")


def test_discover_xlsx_in_folder_is_one_file_not_recursed(tmp_path: Path) -> None:
    wb = Workbook()
    wb.active.append(["a"])
    wb.save(tmp_path / "b.xlsx")
    files = discover(tmp_path)
    # xlsx is PK-magic but classified excel, so it is a single file, never descended into.
    assert [f.name for f in files] == ["b.xlsx"]


def test_discover_unknown_file_still_yielded(tmp_path: Path) -> None:
    (tmp_path / "note.bin").write_bytes(b"\x00\x01random-bytes")
    files = discover(tmp_path)
    # discovery does not filter by format; the matcher decides. Unknown files are yielded
    # (and later go unmatched), never silently dropped at discovery.
    assert [f.name for f in files] == ["note.bin"]


# --- robustness edge cases ------------------------------------------------

def test_discover_empty_folder(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert discover(tmp_path / "empty") == ()


def test_discover_empty_zip_is_opaque_not_crash(tmp_path: Path) -> None:
    ez = tmp_path / "empty.zip"
    with zipfile.ZipFile(ez, "w"):
        pass
    files = discover(ez)                                    # PK\x05\x06, not a local header
    assert [f.name for f in files] == ["empty.zip"]        # opaque single file, no crash


def test_discover_corrupt_direct_archive_raises_clear_error(tmp_path: Path) -> None:
    cz = tmp_path / "corrupt.zip"
    cz.write_bytes(b"PK\x03\x04" + b"\x00" * 40)            # archive magic, invalid body
    with pytest.raises(ValueError, match="not a readable zip"):
        discover(cz)


def test_discover_corrupt_archive_in_folder_is_survived_and_warned(tmp_path: Path, caplog) -> None:
    (tmp_path / "corrupt.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 40)
    (tmp_path / "ok.csv").write_text("a\n1\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        files = discover(tmp_path)
    names = sorted(f.name for f in files)
    assert names == ["corrupt.zip", "ok.csv"]              # run continues, other files found
    assert any("could not open archive" in r.message for r in caplog.records)


def test_discover_normalises_windows_backslash_arcnames(tmp_path: Path) -> None:
    wz = tmp_path / "win.zip"
    with zipfile.ZipFile(wz, "w") as z:
        z.writestr(zipfile.ZipInfo("dir\\sub\\file.bin"), b"x")
    files = discover(wz)
    assert [str(f.logical_path) for f in files] == ["dir/sub/file.bin"]
