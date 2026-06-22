"""Unit tests for the FormatReaders: csv/excel/sqlite read params, recovery_state,
peek_columns, and list_subtables — the per-format behaviour behind the uniform API."""
from __future__ import annotations

import sqlite3
from pathlib import Path, PurePosixPath

import pytest
from openpyxl import Workbook

from model_atlas.sources.container import FilesystemContainer, SourceFile
from model_atlas.sources.readers.csv_reader import CsvReader
from model_atlas.sources.readers.excel_reader import ExcelReader
from model_atlas.sources.readers.sqlite_reader import SqliteReader


def _file(root: Path, rel: str) -> SourceFile:
    return SourceFile(containers=(FilesystemContainer(root),), logical_path=PurePosixPath(rel))


def _xlsx(path: Path, sheets: dict[str, tuple[list, list[list]]]) -> Path:
    wb = Workbook()
    first = True
    for name, (header, rows) in sheets.items():
        ws = wb.active if first else wb.create_sheet()
        ws.title = name
        first = False
        ws.append(header)
        for row in rows:
            ws.append(row)
    wb.save(path)
    return path


def _db(path: Path, table: str = "t", rows: int = 2) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"CREATE TABLE {table} (id INTEGER, lat REAL, lon REAL)")
        for i in range(rows):
            conn.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", (i, 1.0 * i, 2.0 * i))
        conn.commit()
    finally:
        conn.close()
    return path


# --- CSV ------------------------------------------------------------------

def test_csv_reader_honours_delimiter(tmp_path: Path) -> None:
    (tmp_path / "d.csv").write_text("a;b\n1;2\n3;4\n", encoding="utf-8")
    res = CsvReader().read(_file(tmp_path, "d.csv"), {"delimiter": ";"})
    assert list(res.dataframe.columns) == ["a", "b"]
    assert len(res.dataframe) == 2
    assert res.source_columns == ("a", "b")
    assert list(res.recovery_state) == ["live", "live"]


def test_csv_reader_skiprows(tmp_path: Path) -> None:
    (tmp_path / "d.csv").write_text("junk line\na,b\n1,2\n", encoding="utf-8")
    res = CsvReader().read(_file(tmp_path, "d.csv"), {"skip_rows": 1})
    assert list(res.dataframe.columns) == ["a", "b"]
    assert len(res.dataframe) == 1


def test_csv_peek_columns_and_no_subtables(tmp_path: Path) -> None:
    (tmp_path / "d.csv").write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    reader, file = CsvReader(), _file(tmp_path, "d.csv")
    assert reader.peek_columns(file) == {"a", "b", "c"}
    assert reader.list_subtables(file) == []


# --- Excel ----------------------------------------------------------------

def test_excel_reader_reads_requested_sheet(tmp_path: Path) -> None:
    _xlsx(tmp_path / "b.xlsx", {
        "S1": (["x"], [[1]]),
        "Data": (["a", "b"], [[1, 2], [3, 4]]),
    })
    file = _file(tmp_path, "b.xlsx")
    res = ExcelReader().read(file, {"sheet": "Data"})
    assert list(res.dataframe.columns) == ["a", "b"]
    assert len(res.dataframe) == 2
    assert list(res.recovery_state) == ["live", "live"]


def test_excel_peek_columns_and_subtables(tmp_path: Path) -> None:
    _xlsx(tmp_path / "b.xlsx", {"S1": (["x"], [[1]]), "Data": (["a", "b"], [[1, 2]])})
    reader, file = ExcelReader(), _file(tmp_path, "b.xlsx")

    class _Sel:
        sheet = "Data"

    assert reader.peek_columns(file, _Sel()) == {"a", "b"}
    assert set(reader.list_subtables(file)) == {"S1", "Data"}


# --- SQLite ---------------------------------------------------------------

def test_sqlite_reader_table(tmp_path: Path) -> None:
    _db(tmp_path / "x.sqlite")
    res = SqliteReader().read(_file(tmp_path, "x.sqlite"), {"table": "t"})
    assert len(res.dataframe) == 2
    assert list(res.recovery_state) == ["live", "live"]
    assert "_meta_sqlite_source" not in res.dataframe.columns
    assert res.metadata["row_count_merged"] == 2
    assert res.metadata["table"] == "t"


def test_sqlite_list_subtables_and_peek(tmp_path: Path) -> None:
    _db(tmp_path / "x.sqlite")
    reader, file = SqliteReader(), _file(tmp_path, "x.sqlite")
    assert "t" in reader.list_subtables(file)

    class _Sel:
        table = "t"

    assert reader.peek_columns(file, _Sel()) == {"id", "lat", "lon"}


def test_sqlite_sql_query_path(tmp_path: Path) -> None:
    _db(tmp_path / "x.sqlite")
    res = SqliteReader().read(_file(tmp_path, "x.sqlite"), {"sql": "SELECT id, lat FROM t WHERE id >= 0"})
    # extract_query returns table-qualified column names (e.g. "t.id", "t.lat").
    assert len(res.dataframe.columns) == 2
    assert {c.split(".")[-1] for c in res.dataframe.columns} == {"id", "lat"}
    assert len(res.dataframe) == 2
    assert res.metadata["subunit"] == "query"
    assert res.metadata["custom_sql"] is not None


def test_sqlite_reader_requires_exactly_one_of_table_or_sql(tmp_path: Path) -> None:
    file = _file(_db(tmp_path / "x.sqlite").parent, "x.sqlite")
    with pytest.raises(ValueError):
        SqliteReader().read(file, {})                       # neither
    with pytest.raises(ValueError):
        SqliteReader().read(file, {"table": "t", "sql": "SELECT 1"})  # both
