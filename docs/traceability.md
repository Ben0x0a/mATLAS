# Traceability and Forensic Notes

The tool is designed to be forensically cautious. It does not make the output
"self-proving"; it records enough context to support review and repeatability.

## Source Integrity

Source files are opened read-only and extraction always works on temporary
copies. The integrity check answers one question: **did this tool modify the
source between the start and end of the run?**

SHA-256 is computed over the full file before and after extraction for every
CSV, Excel, and direct SQLite source. The before/after pair is recorded in the
traceability sidecar. A mismatch is flagged as a warning and recorded in the
sidecar — it does not abort the run.

## Traceability Sidecars

The `process` command writes two JSON sidecars next to each output CSV. For
`output.csv`:

```text
output.matlas.traceability.json
output.matlas.warnings.json
```

### Human-readable traceability (default)

`output.matlas.traceability.json` contains:

| Field | Content |
| --- | --- |
| `tool` | Name, pipeline version, and app tag |
| `run.started_at` / `run.finished_at` | ISO-8601 UTC timestamps |
| `run.input` | Input path passed to `--input` |
| `run.presets` | Preset path passed to `--presets` |
| `run.output_csv` | Output CSV path |
| `row_counts` | Counts of sources, matched, total rows, and ranked rows |
| `warnings` | Number of transform warnings |
| `sources` | Per-source record (see below) |

Each entry in `sources` records:

- `source_file` — logical source name;
- `source_file_path` — original path on disk;
- `matched_preset` — preset name that selected this source;
- `parser` — parser name and version from the preset;
- `source_tier` — evidential tier declared in the preset;
- `record_count` — rows extracted from the source;
- `assertion_count` — assertion rows produced after mapping;
- `frontier` — P/E/M frontier report (see below).

### PROV-JSON traceability (`--traceability-format prov`)

Pass `--traceability-format prov` to get a W3C PROV-JSON record instead. The
sidecar uses the `matlas:` namespace prefix. It records the run as a
PROV `activity`, each source as a PROV `entity`, and the tool as a PROV
`agent`. The structure is machine-consumable by PROV-aware tooling.

In split mode (`--no-merge`), each per-preset CSV receives its own traceability
and warnings sidecars.

### Warnings sidecar

`output.matlas.warnings.json` contains:

- `transform_warnings` — list of per-row mapping warnings;
- `transform_warning_count` — total warning count;
- `frontier` — per-source P/E/M frontier (same data as in the traceability
  sidecar, here for quick inspection without opening the larger file).

## P/E/M Frontier

Every matched source produces a frontier report comparing three column sets:

| Set | Meaning |
| --- | --- |
| **P** resent | Columns actually in the source file |
| **E** xpected | Columns declared in `expected_columns` in the preset |
| **M** apped | Columns consumed by at least one field mapping |

Derived gaps:

| Key | Meaning |
| --- | --- |
| `frontier_known` | Present ∩ Expected − Mapped — inventoried but not yet used |
| `frontier_new` | Present − Expected − Mapped — unexpected columns to investigate |
| `drift_missing` | Expected − Present — declared columns absent from this source |
| `mapped_absent` | Mapped − Present — a preset mapping references a missing column |

`frontier_known` is the research backlog. `frontier_new` signals schema drift
or a new data field. `drift_missing` signals source schema change.
`mapped_absent` is a preset authoring issue to review; the missing value resolves
to `None` unless a later pipe raises.

## SQLite Helper Metadata

The SQLite adapter may add two helper columns to the extracted DataFrame before
source-agnostic mapping. These are not integration-model columns and are not
written to the output CSV unless a preset explicitly maps them.

| Column | Meaning |
| --- | --- |
| `_meta_sqlite_source` | Row origin: `both`, `wal`, `journal`, or `db_only_unique` |
| `_meta_sqlite_source_row_number` | 1-based row number in the extracted result set |

## What the Tool Does Not Prove

The traceability sidecar does not replace an evidence handling record. It does
not prove:

- who physically handled the device or extraction;
- how the source was acquired;
- that the host machine was trusted;
- that the Python environment was externally validated;
- that SQLite itself is bug-free.

For legal or formal forensic use, pair the output with acquisition logs,
operator notes, chain-of-custody documents, validated tool versions, and
independent review.
