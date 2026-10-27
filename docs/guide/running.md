# Running mATLAS

The full operating guide: how a case grows run after run, every `process` option, what each
output is, the GUI, presets, configuration and profiles. The root [README](../../README.md)
keeps only the quick start.

## The case & append model

One case lives in one `<name>.matlas.sqlite`. Running against a folder where that file does
**not** exist **creates** the case; running again against the **same** `--output`/`--name`
**appends** the new evidence to the existing case (a new run under the same case identity).
Every export re-projects the whole case, so the CSV always reflects everything accumulated so
far. Re-adding evidence already in the case is a recorded skip, never a duplicate or a crash.

`--case "Operation Falcon"` optionally stores a human case label on creation (it defaults to
`--name`); on append the label is read from the existing database.

`--concerns` is required: it names the case subject every observation is attributed to.
`--entity` is optional. When supplied, these arguments **override** any `entity`/`concerns`
a preset maps; the preset value is the default used only when the argument is absent.

## Useful options

```bash
python matlas.py process --help
python matlas.py process --input ./evidence --presets ../presets/mATLAS-presets --output ./out --concerns Subject --export kml --export geojson
python matlas.py process --input ./evidence --presets ../presets/mATLAS-presets --output ./out --concerns Subject --traceability-format prov
python matlas.py process --input ./evidence --presets ../presets/mATLAS-presets --output ./out --concerns Subject --no-source-columns
python matlas.py process --input ./evidence --profile ./profiles/ios-locations.mATLAS-profile --output ./out --concerns Subject
python matlas.py --log-level DEBUG process --input ./evidence --presets ../presets/mATLAS-presets --output ./out --concerns Subject
```

`--export NAME` (repeatable) writes an additional projection alongside the CSV — one file per
format (`kml`, `geojson`, `xlsx`, `geovisualizer`, `sqlite_db`). By default every original
source column is appended verbatim, prefixed `orig_` (e.g. `orig_Latitude`), after the canonical
columns; `--no-source-columns` emits the canonical columns only.

When `--input` is a single file that resolves to exactly one source and `--presets` is a single
YAML, mATLAS runs in **force-preset mode**: it applies that preset without selector matching,
which is handy for testing a new preset against one file.

`--log-level INFO` (the default) records the main processing phases; `--log-level DEBUG`
records precise discovery, preset matching, extraction, mapping, timestamp conversion, and
export steps. Logs go to stderr and to `--log-file` when supplied.

## Outputs (written inside `--output`, stem from `--name`)

- `<name>.matlas.sqlite` — the case database (always written; it is the run itself)
- `<name>.csv` — the flat observation CSV (unless the csv export is deselected)
- `<name>.matlas.traceability.json` and `<name>.matlas.warnings.json` — the sidecars
- one file per additional `--export`

The flat CSV column contract is [reference/csv_contract.md](../reference/csv_contract.md).

## GUI

```bash
python -m pip install '.[gui]'
python matlas.py          # or: python matlas.py gui
```

The GUI is case-first: **New case** / **Open case** controls, a case-name field that becomes
the case database identity (so a run against an existing case appends to it), and a summary
that grows as evidence is added. Three tabs share the open case — **Transform** (add evidence),
**Database Edit**, and **Spatiotemporal Analysis**. GUI files implement no core logic; they
call the same package API as the CLI.

## Presets

Presets are YAML files loaded recursively from the folder passed with `--presets`, or loaded
directly when `--presets` points to one YAML file. `--presets` has no default in the CLI — the
GUI defaults to `<current folder>/presets/mATLAS-presets`, matching the sibling-clone layout of
the installation. A preset has:

- a `preset` header (id, name, os/tool, version, os_version, tier);
- an `input_selector` identifying the source (CSV, Excel, SQLite or XML, direct or inside a ZIP);
- `common` row-level fields plus `observations` (each a `position` + `time` + `links`);
- explicit value references (`field()`, `header()`, `filename()`, `param()`, `const()`), named
  timestamp codecs (`epoch:`), declarative `unit:` conversion, and `pipe` chains for procedural
  cases.

Authoring is documented in [preset_authoring.md](preset_authoring.md) and the full YAML
reference in [preset_schema.md](preset_schema.md).

**Presets can go stale.** A preset encodes one mapping of one tool/source's export format at
the time it was written; source formats change across tool and OS versions. Before relying on
a preset's output in a real case, check its `preset.version`/`os_version`, confirm your
export's columns still match its `expected_fields`, and lint it:

```bash
python utils/lint_presets.py ../presets/mATLAS-presets
```

## Configuration (`matlas_config.toml`)

Set-once preferences (not per-run options) live in an optional `matlas_config.toml` in the
directory you run from. Copy `matlas_config.toml.example` to start. When absent, built-in
defaults are used; a present-but-invalid value (bad IANA zone, unknown key) is a hard error.

The most important key is the **local timezone**, used to show times in the case's zone:

```toml
[timezone]
local_zone = "Europe/Paris"   # IANA name; unset => zone unknown
```

mATLAS records that zone's **DST-aware** offset at each row's instant; the canonical UTC
timestamps stay UTC regardless. Other sections set logging (`level`/`format`), discovery
depths, the `orig_` passthrough prefix, and the crash-report `[diagnostics]` behaviour.
Security safeguards (zip-bomb guards) are deliberately **not** configurable here.

## Profiles

Profiles are `.mATLAS-profile` JSON files that store selected preset YAML paths — a CLI/GUI
convenience for curated preset sets. Pass one with `--profile` instead of `--presets`.

## Forensic defaults

- No reader parses the original file in place: every non-archive source is copied to a
  temporary directory and read from the copy (SQLite stages its WAL/SHM/journal siblings too),
  and the original's hash is recorded.
- Evidence is stored verbatim; every conversion is a separate, method-stamped derivation.
- Nothing is dropped silently: an unreadable source, an unmatched preset, a skipped duplicate,
  and an unmapped field are each warned or recorded.
- Two byte-identical exhibits at different paths are two sources; sameness is a query, never
  an automatic merge.
- Source file, original path, parser, preset, warnings, and execution context are recorded in
  the sidecar files and the case database.

The processing conventions are catalogued in [reference/conventions.md](../reference/conventions.md).
