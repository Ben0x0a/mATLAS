# Transform Behavior

Transforms run after source extraction and before export. The public entry point
is `model_atlas.pipeline.process(input_path, presets_path, output, ...)`.

Processing order:

1. Load preset specs.
2. Discover source files via the Container model (`discover()`).
3. Match each file to a preset with `match_file()` — its `input_selector` location
   (`name`/`path`) then a magic-verified `format` guard. When several presets match one
   file, tie-break by structural fit (which preset's referenced columns the file actually
   has), then declaration order. (In force-preset mode — one ordinary input file plus one
   preset YAML — location matching is bypassed but `format` is still verified.)
4. Extract a pandas DataFrame through the `SingleSourceExtractor` and the format's
   `FormatReader`. The file is staged to a temp copy first and read from the copy; the
   original is never opened by the parser.
5. Convert rows to plain records with missing values normalized to `None`.
6. Build canonical assertion rows from `common`, `assertions` (`position`/`time`/
   `links`).
7. Apply untangle ranking.
8. Write CSV output plus traceability and warning sidecars.

## Row Assembly

`build_rows()` is source-agnostic. It receives extracted records, a parsed
`PresetSpec`, and provenance supplied by the pipeline.

For each source row:

1. Start with every canonical output column set to `None`.
2. Apply preset `common` fields.
3. Apply run-level `entity`/`linked_entity` overrides (when supplied).
4. Set engine provenance: `input_file_path`, `source_record_number` (the record's 1-based
   ordinal), `preset_id`, `preset_name`; default `raw_source_path` (inner-container logical
   path) and `source_tier`; default `input_record_id` to `<table-or-sheet>#<ordinal>` when
   unmapped.
5. Resolve `source_record_uid` (a mapped tool id, verbatim) or generate a deterministic,
   content-addressed UID for the source record.
6. For each assertion, apply `position` fields (capturing the lat/lon source columns),
   then the `time` block, generate a unique `row_uid` for the row, and emit one output row.

One source row can therefore produce multiple assertion rows (one per assertion).

## Reference Resolution

A mapped value is one explicit call: `column(NAME)`, `header("Glob *")`,
`filename(name|stem|path)`, `param(...)`, or `const(...)`. A `column`/`header`
name may be a glob, which must match exactly one extracted source column.

`header(...)` resolves to the matched column's name itself — useful when metadata is
encoded in a header, e.g. a timezone in `Timestamp Date/Time - UTC+00:00 (dd.MM.yyyy)`.
`filename(...)` yields part of the source file identity, so a row can carry
filename-derived and column-derived values together.

Numeric model columns are cast automatically from their declared type; `unit:`
converts a declared source unit to the model's canonical unit; `extract: pattern.group`
pulls a named regex group. Only genuinely procedural needs use a `pipe`.

## Entity And Linked Entity

`process(..., entity=, linked_entity=)` are run-level values. When supplied they
**override** whatever a preset maps for `entity` / `linked_entity`; the preset
mapping is the default used only when the corresponding argument is absent. The CLI
exposes these as `--entity` and `--linked-entity`.

## Pipes

A pipe is a left-to-right call chain string, e.g. `"cast(int) | scale(3.6)"`. Steps:
`cast`, `scale`, `arithmetic`, `lookup` (by named `lookup_tables` entry), `regex`
(a named group of a named `patterns` entry), `split`. Every step treats `None` as a
no-op and accepts `on_error=null|raw|error` (default `null`).

## Timestamps

A `time` block decodes to Unix microseconds via a named `epoch`
(`unix_s|unix_ms|unix_us|unix_ns|cocoa|webkit`) or a `format` (strptime); with neither
the value is assumed to already be Unix µs. For `format`, a naive datetime uses `zone`
(a number of hours or an offset string like `"UTC+02:00"`), and a `%z` in the format is
honoured as parsed.

When the `time` block captures `zone` (a constant, or `header(...)` + `regex` on a
header), that offset is applied during parsing — so a zone embedded in a column header
is *applied*, not merely recorded into `time_zone`. An unparseable captured zone is
ignored (the value still parses as UTC).

## Temporal Expansion

An `instant` writes the same raw value, source field, and normalized value to both
lower and upper bounds. An `interval` uses separate `lower`/`upper` column references.

## Record & row UIDs

Two distinct identifiers:

- **`source_record_number`** — the engine-set 1-based ordinal of the record within its
  extracted source. Surfaced as a column so an analyst can jump to the record, and the
  always-unique disambiguator behind the UIDs.
- **`source_record_uid`** — one per SOURCE record, **shared** by every output row a record
  fans out into. A preset may map it to a genuine source UID (a tool's Item ID); a *mapped*
  value that repeats across distinct records is a hard error (a non-unique "stable id" is a
  preset bug). When the preset maps none, mATLAS generates a deterministic uuid5 from
  provenance (fingerprint + raw_source_path + source_record_number) — never colliding, since
  the record number is unique.
- **`row_uid`** — engine-generated, **unique per OUTPUT row** by construction: a
  deterministic uuid5 over the row's own DATA + `source_record_number` + the output-row
  ordinal (scoped by the source identity): content-addressed, yet identical-data rows stay
  distinct via the record number and a record's fan-out rows via the output ordinal. Never mapped by a preset.

## Merge And Split Output

Merge mode is the default. It concatenates all matched preset frames, runs
untangle over the merged model, and writes one CSV plus sidecars:

```bash
python matlas.py process --input ./evidence --presets ./presets --output ./out/merged.csv
```

Split mode writes one CSV per matched preset into the output folder and creates
sidecars next to each CSV:

```bash
python matlas.py process --input ./evidence --presets ./presets --output ./out/by-preset --no-merge
```

## Untangle

Untangle groups comparable rows by:

- `time_lower_unix_us` truncated to seconds
- `time_upper_unix_us` truncated to seconds
- `entity`
- `entity_time_link`
- `spatial_temporal_link`

Rows are ranked by best available horizontal accuracy first. If accuracy cannot
decide, the row with more populated useful fields wins. Remaining ties preserve
input order.

Untangle writes:

- `record_type`
- `record_rank`
