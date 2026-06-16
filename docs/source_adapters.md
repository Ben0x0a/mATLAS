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

Excel selectors use file name and sheet name. CSV selectors use file name.

CSV and Excel adapters hash the source file before and after reading. SQLite
direct files use full hashes; SQLite-in-ZIP uses the strategic ZIP fingerprint.
