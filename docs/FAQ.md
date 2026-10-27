# FAQ

Common technical questions and fixes. For how a run works end to end, see
[guide/running.md](guide/running.md).

## Running

### A source was found but no preset matched it

Run with `--log-level DEBUG` to see why each preset was accepted or rejected. The usual causes:

- **`input_selector.name` is an exact file name.** A wildcard there matches nothing; the
  linter reports it as `glob-in-selector-name`.
- **The format is checked from the file's content**, not its extension: a `.csv` that is
  really an Excel workbook will not match a `format: csv` selector.
- **A required column is missing** from the export: compare it with the preset's
  `expected_fields`.

Lint the presets before trusting them:

```bash
python utils/lint_presets.py ../presets/mATLAS-presets
```

### "… is not a mATLAS run database (… user_version=4; expected … / 5)"

The case database was written by an older schema version. A case database is never migrated
in place: point `--output`/`--name` at a new case and re-ingest the evidence.

### I ran the same input twice and nothing was added

By design. Evidence already in the case is recorded as a skip, never added twice, so
appending is safe to repeat.

### Times end in `N/A`

No local zone is configured, so the offset from UTC to the case's local time is unknown.
Set it in `matlas_config.toml`:

```toml
[timezone]
local_zone = "Europe/Paris"
```

The stored UTC timestamps are unaffected; only how times are shown changes.

### CDR or ANPR rows have no coordinates

Cell and camera identifiers are kept verbatim, but resolving them to coordinates through a
register is not implemented yet. The bearer (antenna, camera) is recorded, without a position.

### The CSV has too many `orig_*` columns

Those are the source columns copied verbatim. Add `--no-source-columns` for the canonical
columns only.

### The concomitance analysis raises `NotImplementedError`

It is disabled on purpose until its presence test is redesigned for the current link model
(decision 55). Nothing is wrong with the installation.

## Reading the output

### Why is the bearer the same as the entity?

For `entity_link = subject` the entity holds its own position (a phone's GNSS fix), so it is
its own bearer. For `party` the bearer is the other entity (the antenna, the camera); for
`unknown` there is none.

### Where is "the device was NOT there" recorded?

In `temporal_link`: `never` (not there at any time of the interval) or `not_throughout`
(absent at some point). It is the only column that can state a negation.

### Two observations have the same `spatiotemporal_entity_hash`

They may describe the same evidence (for example two disclosures of one record). It is a
finding to review, never an automatic merge.

## Installation and development

### The GUI does not start

Install the GUI extra: `python -m pip install '.[gui]'`.

### Where are crash reports?

On an unhandled error mATLAS writes `matlas_error_<time>_p<pid>.json` and `.md` into
`crash_reports/` (relative to the folder you ran from; configurable in
`matlas_config.toml` `[diagnostics]`). The report has a shareable `safe` section and a
`sensitive` section to review before sharing.

### How do I run the tests?

With the project's own environment:

```bash
.venv/bin/python -m pytest
```
