# Model Atlas

Model Atlas is a clone-and-run app for turning heterogeneous forensic data
exports into one spatio-temporal integration model.

The core is CLI-first and source-agnostic. It reads CSV files, Excel workbooks,
and SQLite databases, matches them to YAML presets, maps source columns into a
flat spatio-temporal assertion model, and writes CSV output plus warning and
traceability sidecars. A PySide6 GUI is available as an optional front-end.

## Requirements

- Python 3.11 to 3.14
- `pandas`
- `pyyaml`
- `openpyxl`

The GUI dependency is optional and installed through the `gui` extra.

## CLI Run

From a clone of this folder:

```bash
python matlas.py process \
  --input ./evidence \
  --presets ./presets \
  --output ./out/merged.csv \
  --linked-entity "Case Subject"
```

`--linked-entity` is required: it names the case subject every output row is
attributed to. `--entity` is optional. When supplied, these arguments **override**
any `entity`/`linked_entity` a preset maps; the preset value is the default used
only when the argument is absent.

Useful options:

```bash
python matlas.py process --help
python matlas.py --log-level DEBUG process --input ./evidence --presets ./presets --output ./out/merged.csv --linked-entity Subject
python matlas.py process --input ./evidence --presets ./presets --output ./out/merged.csv --linked-entity Subject --entity iPhone
python matlas.py process --input ./evidence --presets ./presets --output ./out/merged.csv --linked-entity Subject --traceability-format prov
python matlas.py process --input ./evidence --presets ./presets --output ./out/by-preset --no-merge --linked-entity Subject
python matlas.py process --input ./evidence --profile ./profiles/ios-locations.mATLAS-profile --output ./out/merged.csv --linked-entity Subject
```

When `--input` is a single file that resolves to exactly one source element and
`--presets` is a single YAML, mATLAS runs in **force-preset mode**: it applies that
preset without selector matching, which is handy for testing a new preset against
one file.

Merge-mode outputs:

- `merged.csv`
- `merged.matlas.traceability.json`
- `merged.matlas.warnings.json`

In split mode (`--no-merge`), `--output` is a folder and mATLAS writes one CSV
per matched preset with sidecars next to each CSV.

`--log-level INFO` is the default and records the main processing phases.
`--log-level DEBUG` records precise discovery, preset matching, extraction,
mapping, timestamp conversion, untangle, and export steps. Logs are written to
stderr and to `--log-file` when supplied.

## GUI

Install the GUI extra, then launch with no arguments or with `gui`:

```bash
python -m pip install '.[gui]'
python matlas.py
python matlas.py gui
```

For CLI help when launching with no arguments, use:

```bash
python matlas.py --help
```

The GUI exposes source selection, preset folder/YAML selection, auto matching,
manual preset selection, profile load/save, merge/split output mode, log level,
traceability format, and a log window. GUI files do not implement core preset,
source, transform, export, or traceability logic; they call the same package API
as the CLI.

## Presets

Presets are YAML files loaded recursively from the folder passed with
`--presets`, or loaded directly when `--presets` points to one YAML file. A v3
preset has:

- a `preset` header (id, name, os/tool, version, os_version, tier);
- a `match` block identifying the source (CSV, Excel, or SQLite, direct or in a ZIP);
- `common` row-level fields plus `assertions` (each a `position` + `time` + `links`);
- explicit value references (`column()`, `header()`, `filename()`, `param()`, `const()`),
  named timestamp codecs (`epoch:`), declarative `unit:` conversion, and `pipe` chains
  for the procedural cases.

Example presets live under `presets/axiom/` (AXIOM CSV exports) and `presets/ios/`
— direct SQLite presets read from a full-filesystem ZIP, e.g.
`ios_routined_cached_locations.yaml` (`com.apple.routined/Cache.sqlite` CLLocation
points) and `ios_routined_significant_visits.yaml`
(`com.apple.routined/Local.sqlite` significant-location dwell intervals).
The canonical model is documented in `docs/integration_model.md`; preset YAML
logic is documented in `docs/preset_schema.md` and
`docs/preset_authoring_quickstart.md`.

## Profiles

Profiles are `.mATLAS-profile` JSON files that store selected preset YAML paths.
They are a CLI/GUI convenience for curated preset sets.

```bash
python matlas.py process \
  --input ./evidence \
  --profile ./profiles/ios-locations.mATLAS-profile \
  --output ./out/merged.csv
```

## Forensic Defaults

- No adapter parses the original file in place: every non-archive source is copied
  to a temporary directory and read from the copy, and the original's hash is
  recorded before and after the run. (SQLite stages its WAL/SHM/journal siblings too.)
- Unmatched source elements are warned and skipped.
- Expected-column drift is warned.
- Missing mapped columns resolve to `None` unless a later pipe raises.
- Duplicate explicit `source_row_id` values across different source rows are
  hard errors.
- Source file, original path, parser, preset, warning, and execution context are
  recorded in sidecar files.

Developer and implementation documentation lives in `docs/`.

## Development & AI use

Generative AI was used in this project mainly to assist during the coding phase.
The original ideas and the overall structure are the owner's, and all core logic
has been reviewed. Even so, mistakes or bugs may have slipped past proof-reading
- please report anything unexpected.
