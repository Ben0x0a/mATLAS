# Model Atlas

Model Atlas is a clone-and-run command-line app for turning heterogeneous
forensic data exports into one spatio-temporal integration model.

The first supported processing path is CLI-first. It reads CSV files, Excel
workbooks, and SQLite databases, matches them to YAML presets, maps source
columns into a flat spatio-temporal assertion model, and writes a merged CSV
plus warning and traceability sidecars.

## Requirements

- Python 3.11 to 3.14
- `pandas`
- `pyyaml`
- `openpyxl`

The GUI dependency is optional and installed separately through the `gui` extra.

## Run

From a clone of this folder:

```bash
python matlas.py process \
  --input ./evidence \
  --presets ./presets \
  --output ./out/merged.csv
```

Useful options:

```bash
python matlas.py process --help
python matlas.py --log-level DEBUG process --input ./evidence --presets ./presets --output ./out/merged.csv
python matlas.py process --input ./evidence --presets ./presets --output ./out/merged.csv --traceability-format prov
```

Outputs:

- `merged.csv`
- `merged.matlas.traceability.json`
- `merged.matlas.warnings.json`

`--log-level INFO` is the default and records the main processing phases.
`--log-level DEBUG` records precise discovery, preset matching, extraction,
mapping, timestamp conversion, untangle, and export steps. Logs are written to
stderr and to the default CLI log file unless `--log-file` is supplied.

## Presets

Presets are YAML files loaded recursively from the folder passed with
`--presets`. They describe:

- how to identify a source;
- how to extract data from CSV, Excel, or SQLite;
- expected source columns;
- how to map source columns into the integration model;
- how to derive timestamps, labels, and provenance.

Example presets live under `presets/spatiotemporal/`.
The canonical model is documented in `docs/integration_model.md`; preset YAML
logic is documented separately in `docs/preset_schema.md`.

## Forensic Defaults

- Unmatched source elements are warned and skipped.
- Expected-column drift is warned.
- Missing columns used by mappings are errors.
- Appending keeps all previous and new rows. The app does not deduplicate rows
  because silent deletion is worse than duplicated evidence in this workflow.
- Source file, original path, raw-data reference, parser, preset, warning, and
  execution context are recorded in sidecar files.

Developer and implementation documentation lives in `docs/`.

## Development & AI use

Generative AI was used in this project mainly to assist during the coding phase.
The original ideas and the overall structure are the owner's, and all core logic
has been reviewed. Even so, mistakes or bugs may have slipped past proof-reading —
please report anything unexpected.
