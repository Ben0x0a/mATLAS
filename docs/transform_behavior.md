# Transform Behavior

Transforms run after source extraction and before export. The public entry point
is `model_atlas.pipeline.process(input_path, presets_path, output, ...)`.

Processing order:

1. Load preset specs.
2. Discover source elements.
3. Match each source element to the first preset whose selector applies. (In
   force-preset mode — one input file yielding one element plus one preset YAML —
   matching is bypassed and the single preset is applied directly.)
4. Extract a pandas DataFrame through the matching source adapter. Non-archive
   sources are copied to a temp directory first and read from the copy; the
   original is never opened by the parser.
5. Convert rows to plain records with missing values normalized to `None`.
6. Build canonical assertion rows from `common`, `assertions`, and temporal
   specs.
7. Apply untangle ranking.
8. Write CSV output plus traceability and warning sidecars.

## Row Assembly

`build_rows()` is source-agnostic. It receives extracted records, a parsed
`PresetSpec`, and provenance supplied by the pipeline.

For each source row:

1. Start with every canonical output column set to `None`.
2. Apply preset `common` fields.
3. Fill missing provenance defaults: `acquisition_path`, `source_file_path`,
   `input_file`, and `source_tier`.
4. Resolve or generate `source_row_id`.
5. For each assertion template, apply assertion fields.
6. For each temporal spec, capture raw temporal values, apply temporal pipes,
   and emit one final output row.

One source row can therefore produce multiple assertion rows.

## Column Resolution

Preset `from` and temporal column references may be exact names or glob
patterns. A glob must match exactly one extracted source column.

`from_name` resolves to the matched source column name itself. This is useful
when metadata is encoded in a header, for example a timezone in
`Timestamp Date/Time - UTC+00:00 (dd.MM.yyyy)`.

`from_file` resolves to part of the source file identity — `name`, `stem`, or
`path` — instead of a column, so a row can carry filename-derived and
column-derived values together.

## Entity And Linked Entity

`process(..., entity=, linked_entity=)` are run-level values. When supplied they
**override** whatever a preset maps for `entity` / `linked_entity`; the preset
mapping is the default used only when the corresponding argument is absent. The CLI
exposes these as `--entity` and `--linked-entity`.

## Pipes

Pipes run left to right. Built-in pipes are:

- `cast`
- `parse_datetime`
- `arithmetic`
- `lookup`
- `regex_extract`
- `split`

All built-ins treat `None` as a no-op so empty cells stay empty unless an
authoring choice explicitly raises an error.

`parse_datetime` writes Unix nanoseconds. A naive datetime is interpreted using
`tz_offset_hours` (a number of hours or an offset string like `"UTC+02:00"`),
defaulting to UTC; a format containing `%z` is honoured as parsed.

When a temporal spec captures `time_zone` (for example via `from_name` +
`regex_extract` on a header, or a constant), the engine feeds that offset into the
spec's `parse_datetime` step automatically — so a zone embedded in a column header
is *applied*, not merely recorded. An explicit `tz_offset_hours` or a `%z` in the
format takes precedence, and an unparseable captured zone is ignored (the value
still parses as UTC).

## Temporal Expansion

An instant temporal spec:

```yaml
temporal:
  - instant: "Recorded At"
```

writes the same raw value, source field, and normalized value to both lower and
upper temporal bounds.

An interval temporal spec:

```yaml
temporal:
  - interval:
      lower: "First Seen"
      upper: "Last Seen"
```

uses separate lower and upper source columns. Temporal overrides such as
`entity_time_link`, `spatial_temporal_link`, and `time_zone` apply only to that
temporal output row.

## Source Row IDs

If a preset declares `source_row_id`, that mapping is used. Distinct source rows
must not resolve to the same ID; a collision is a hard error because it breaks
traceability.

If `source_row_id` is omitted, mATLAS generates a deterministic UUID from
available provenance fields.

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

- `time_lower_unix_ns` truncated to seconds
- `time_upper_unix_ns` truncated to seconds
- `entity`
- `entity_time_link`
- `spatial_temporal_link`

Rows are ranked by best available horizontal accuracy first. If accuracy cannot
decide, the row with more populated useful fields wins. Remaining ties preserve
input order.

Untangle writes:

- `record_type`
- `record_rank`
