# Extending mATLAS: adding a reader and an exporter

mATLAS is built as small **bricks** around a pure core. A capability is added by writing one
module that satisfies a typed protocol and making one registration call — no edit to the core,
the pipeline, or any wiring file. Two brick kinds are described here:

- a **Reader** turns a source file of some format into verbatim records the pipeline can ingest;
- an **Exporter** turns the finished run database into an output file.

Everything flows *inward* toward the core: a reader and an exporter depend on the core model, never
the other way round.

---

## Adding a reader (a new input format)

A reader is one ingest brick per **format** (CSV, Excel, SQLite, XML, …). Containers such as zips
are resolved before your reader is called: it always receives a path to a real, opened unit.

### The `Reader` protocol

Defined in `model_atlas/ingest/readers/base.py`:

```python
class Reader(Protocol):
    format: str

    def subunits(self, source: Path) -> list[str]: ...

    def iter_records(
        self, source: Path, subunit: str | None, params: dict[str, Any] | None = None,
    ) -> Iterator[RawRecord]: ...

    def peek_fields(self, source: Path, subunit: str | None = None) -> set[str] | None: ...
```

- **`format`** — the format name your reader claims (must be unique; it is the registry key).
- **`subunits(source)`** — the named units inside one file: SQLite tables, Excel sheets, XML model
  types. Return `[]` for a format with no sub-unit concept (CSV is one implicit unit).
- **`iter_records(source, subunit, params)`** — yield one `RawRecord` per source row, **in a pinned
  enumeration order**, with values kept **verbatim** (source text, or native SQLite types where
  float precision matters — never re-formatted). `params` carries reader-specific options a preset
  passed through.
- **`peek_fields(source, subunit)`** — the set of field (column) names, used by preset matching to
  check a preset's required fields are present. Return `None` if you cannot cheaply peek.

`RawRecord` (same module) is what you yield:

```python
@dataclass(frozen=True)
class RawRecord:
    line_no: int                       # the row's position in the pinned enumeration order
    fields: dict[str, Any]             # {column_name: verbatim value}
    recovery_state: RecordState = RecordState.LIVE   # override to RECOVERED for e.g. WAL-only rows
```

Your reader assigns **no ids**: `line_no` plus the verbatim `fields` are all a reader can know. The
ingest layer derives the persisted record's identity later.

### Register it

Registration is a class decorator at the definition site — no separate wiring file:

```python
from model_atlas.ingest.readers.registry import register_reader

@register_reader
class MyFormatReader:
    format = "myformat"

    def subunits(self, source): ...
    def iter_records(self, source, subunit, params=None): ...
    def peek_fields(self, source, subunit=None): ...
```

The registry (`model_atlas/ingest/readers/registry.py`) maps `format -> Reader`; `get_reader(fmt)`
looks yours up once it is imported.

### Make the format detectable

Detection is header-first (see the conventions catalogue). If your format has magic bytes or a
recognisable structure, teach `model_atlas/ingest/format_detect.py` to return your `format` name;
otherwise the pipeline falls back to the file suffix. A format the detector never returns is never
routed to your reader.

### Worked example & test

Read `model_atlas/ingest/readers/csv_reader.py` — the smallest complete reader. For a test, drive
the real `Reader` over a tiny fixture file and assert the yielded `RawRecord`s: their `line_no`
order, the verbatim `fields`, and `peek_fields`. Existing reader tests live in `tests/ingest/`.

---

## Adding an exporter (a new output format)

An exporter is one output brick per format. It reads the finished run database and writes files. It
holds only a read-only connection to the store, so it can never mutate evidence.

### The `Exporter` protocol

Defined in `model_atlas/export/csv_flat.py`:

```python
class Exporter(Protocol):
    format: str

    def export(self, store: RunStore, ctx: ExportContext) -> list[Path]: ...
```

- **`format`** — the exporter's format name.
- **`export(store, ctx)`** — query the store, write **one file** (one file per format — there is no
  split mode), and return the paths written.

`ExportContext` carries where and how to write:

```python
@dataclass(frozen=True)
class ExportContext:
    output_dir: Path
    base_name: str = "merged"            # the output file's stem
    ranking_method: str | None = None    # which analysis ranking the projection reads
    include_source_columns: bool = True  # append the verbatim orig_<col> tail
```

### Where your data comes from

Do **not** hand-assemble rows. Project the store through `model_atlas/core/projection.py`:

```python
from model_atlas.core.projection import project

frame = project(store.connection,
                include_source_columns=ctx.include_source_columns,
                ranking_method=ctx.ranking_method)
```

`project` returns the authoritative flat frame — one row per assertion, the canonical column order,
restored numeric dtypes, and the per-run `orig_<col>` tail. Every exporter reads the same
projection, so a value can never differ between two output formats. Your exporter's only job is to
**serialise** that frame into its format.

### Register it

Unlike readers, an exporter registers an **instance** (so it can hold configuration):

```python
from model_atlas.export.registry import register_exporter

register_exporter("myfmt", "My Format", MyFormatExporter(),
                  "One-line description shown in the CLI/GUI export menu.")
```

Add the call where the built-in exporters are registered so `load_builtin_exporters()` picks it up;
then it appears automatically in the CLI `--export` list and the GUI export menu.

### Worked example & test

Read `model_atlas/export/kml.py` — a complete exporter that projects the store, keeps only located
rows, and writes one file. For a test, build a small store (or run the pipeline over a fixture),
call your exporter, and assert the written file's contents. Existing exporter tests live in
`tests/export/`, and `_shared.py` there holds the small value helpers (`clean`, `as_float`, …) the
format bricks reuse.

---

## The one rule to remember

Add a capability by adding **one brick module + one registration call**. If you find yourself
editing the core model, the pipeline, or a central wiring file to add a reader or an exporter,
stop — the brick boundary is there precisely so you do not have to.
