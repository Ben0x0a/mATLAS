# Architecture

The app is split into three layers:

- `matlas.py`: clone-and-run entry script.
- `launcher/`: command dispatch and argument parsing.
- `model_atlas/`: reusable processing package.

All business logic belongs in `model_atlas/`. Launchers construct
typed requests and dispatch to the package.

## Execution Path

```text
matlas.py
  -> launcher.cli
  -> model_atlas.pipeline.process()
  -> discover elements        (sources.folder.discover_elements)
  -> match presets            (presets.matcher.match_preset)
  -> extract DataFrames       (sources adapter registry)
  -> build assertion rows     (transforms.assemble.build_rows)
  -> untangle                 (transforms.rank.untangle)
  -> export CSV, sidecars     (export.write_csv / write_json)
  -> ProcessResult
```

## Package Layout

| Path | Responsibility |
| --- | --- |
| `model_atlas/models.py` | Public typed dataclasses and enums. |
| `model_atlas/pipeline.py` | `SpatioTemporalProcessor` and source-agnostic orchestration. |
| `model_atlas/presets/` | YAML schema, recursive loading, validation, and matching. |
| `model_atlas/sources/` | CSV, Excel, SQLite, and folder discovery adapters. |
| `model_atlas/transforms/` | Model mapping, location expansion, timestamp calculation, details, untangle. |
| `model_atlas/sqlite/` | Small SQLite helper package used by the SQLite source adapter. |
| `model_atlas/export.py` | CSV, warnings, and traceability writers. |
| `launcher/cli.py` | Argparse, request construction, dispatch, summaries, exit codes. |
| `gui/` | Kept MVC GUI shell that calls public package APIs only. |

## Public API

The package-level API is:

```python
from model_atlas import DiscoveredElement, ElementType, ProcessResult, process
```

`process(input_path, presets_path, output_csv, *, traceability_format)` is the
main entry point. It returns a `ProcessResult` with row counts, matched/unmatched
sources, warnings, and output paths. Launchers and GUI workers should call this
function; they should not implement extraction, matching, mapping, export, or
traceability logic themselves.

## Logging

The package logs with the standard-library `logging` module under the
`model_atlas` namespace.

- INFO records the main processing phases: preset loading, discovery, matching,
  extraction, mapping, append, untangle, and output writing.
- DEBUG records forensic trace details: every discovered element, selector
  attempt, column validation decision, row/location expansion, timestamp
  expression calculation, untangle rank decision, sidecar payload construction,
  and evidence hash calculation.

The CLI exposes this through `--log-level`. It writes to stderr by default and
also writes to the path supplied with `--log-file`.

## SQLite Helper Metadata

The integration model uses human-readable output columns documented in
`docs/integration_model.md`. The SQLite source adapter may add `_meta_sqlite_*`
columns to extracted DataFrames before source-agnostic mapping:

| Column | Meaning |
| --- | --- |
| `_meta_sqlite_source` | Origin of the row: `both`, `wal`, `journal`, or `db_only_unique`. |
| `_meta_sqlite_source_row_number` | 1-based source row number before transforms. |
