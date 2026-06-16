# Testing Strategy

Required test groups:

- Public API dataclasses and package imports.
- Recursive preset loading and schema validation.
- Source discovery and adapter extraction for CSV, Excel, and SQLite.
- Preset matching and unmatched-source warnings.
- Expected-column drift warnings and missing mapped-column errors.
- `model_mapping`, `location_mappings`, timestamp interval conversion, temporal
  relation defaults, and position type.
- Details modes.
- Untangle grouping and ranking.
- CLI `python matlas.py process` including output and traceability sidecars.
- SQLite adapter behavior through source-agnostic processing, including direct
  SQLite files and SQLite databases discovered inside ZIP evidence containers.

## Test Layout

| Path | Scope |
| --- | --- |
| `tests/source_agnostic/` | New package pipeline, source-agnostic CLI, presets, AXIOM template utility. |
| `tests/launcher/` | Top-level launcher routing. |
