# Transform Behavior

Transforms run after source extraction and before export. The public entry point
is `model_atlas.pipeline.process(input_path, presets_path, output, ...)`.

Processing order:

1. Load preset specs.
2. Discover source elements.
3. Match each source element to a preset by its `match` block. When several presets
   match, tie-break by structural fit (which preset's referenced columns the source
   actually has), then declaration order. (In force-preset mode — one input file
   yielding one element plus one preset YAML — matching is bypassed.)
4. Extract a pandas DataFrame through the matching source adapter. Non-archive
   sources are copied to a temp directory first and read from the copy; the
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
4. Set engine provenance: `input_file`, `preset_id`, `preset_name`; default
   `source_tier`; default `input_record_id` to `<table-or-sheet>#<ordinal>` when unmapped.
5. Resolve the `record_uid` reference (a mapped tool id, verbatim) or generate a
   deterministic, content-addressed UID.
6. For each assertion, apply `position` fields (capturing the lat/lon source
   columns), then the `time` block, and emit one output row.

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

## Row UIDs

A preset's `row_uid` references a genuine source UID when one exists. Distinct source
rows must not resolve to the same ID; a collision is a hard error because it breaks
traceability. When `row_uid` is omitted, mATLAS generates a deterministic UUID from
available provenance fields (the normal case for raw device databases).

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
