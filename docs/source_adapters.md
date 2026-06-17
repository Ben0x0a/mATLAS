# Source Adapters

Source adapters convert discovered elements into raw pandas DataFrames.

Supported first-slice adapters:

- CSV file
- Excel sheet
- SQLite database table or SQL query
- SQLite database inside a ZIP archive when a preset provides `db_relpath`

Folder discovery scans recursively and skips SQLite sidecar files ending in
`-wal`, `-shm`, or `-journal`. ZIP files are inspected for entries with SQLite
suffixes (`.db`, `.sqlite`, `.sqlite3`) so presets can match the internal
database name/path.

SQLite selectors identify the database/file only. The table or SQL query is
defined under `extract.sqlite`. The adapter uses the migrated forensic SQLite
helpers: it copies evidence to a temporary directory, reads db-only and
db+sidecars views, merges row provenance, validates custom SQL, and records
before/after source integrity metadata.

Excel selectors use file name and sheet name. CSV selectors use file name. A
selector matches when any criterion it declares matches (see
[preset_schema.md](preset_schema.md)).

## Temp Staging (Never Touch The Original)

No adapter parses the original evidence file in place. Every non-archive source is
copied into a throwaway temporary directory first, and the parser reads only the
copy:

- CSV and Excel use the shared `sources/staging.py::stage_file` helper: it hashes
  the original, copies it to a temp dir, and yields the local copy. The original is
  re-hashed after the read to prove it was unchanged.
- SQLite keeps its dedicated `sqlite/locate.py` path (it must also stage WAL/SHM/
  journal siblings and run a two-pass db-only/db+sidecars merge before transforms).

CSV and Excel adapters record the original's hash before and after reading. SQLite
direct files use full hashes; SQLite-in-ZIP uses the strategic ZIP fingerprint.

## Connection Handling (Windows)

SQLite connections are closed explicitly with `contextlib.closing`, both in the
discovery probe (`sources/folder.py`) and in the extractor reads
(`sqlite/extractor.py`). A bare `with sqlite3.connect(...)` only manages the
transaction and leaves the handle open; on Windows that lock blocks the later copy/
open and temp-directory cleanup ("the file is used by someone else").
