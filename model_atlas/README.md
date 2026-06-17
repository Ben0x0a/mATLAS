# `model_atlas` Package

`model_atlas` contains the reusable core logic for the app. It has
no GUI imports and can be used from Python code or from the clone-and-run CLI.

## Public API

```python
from pathlib import Path
from model_atlas import process

result = process(
    input_path=Path("evidence"),
    presets_path=Path("presets"),
    output=Path("out/merged.csv"),
    linked_entity="Case Subject",
)
print(result.row_counts)
```

`process()` discovers sources under `input_path`, matches them to YAML presets,
extracts and maps columns into the canonical assertion model, runs untangle, and
writes CSV output plus `.matlas.traceability.json` and `.matlas.warnings.json`
sidecars. It returns a `ProcessResult` with row counts, matched/unmatched source
lists, warning strings, and output paths.

`linked_entity` is a required keyword argument — the entity every output row is
attributed to. `entity` is optional. When supplied, both override the value a
preset maps (the preset value is the default otherwise). Set `merge=False` to write
one CSV per matched preset into an output folder.

## Execution Path

```text
process()
  -> discover_elements()          (sources.folder)
  -> load_preset_specs()          (presets.spec_loader)
  -> match_preset()               (presets.matcher)
  -> adapter.extract()            (sources registry)
  -> build_rows()                 (transforms.assemble)
  -> untangle()                   (transforms.rank)
  -> write_csv() / write_json()   (export)
  -> ProcessResult
```

The package logs under the `model_atlas` namespace. INFO records high-level
phases; DEBUG records discovery decisions, selector attempts, column validation,
row expansion, timestamp expressions, untangle ranking, and sidecar payloads.

## Development & AI use

Generative AI was used in this project mainly to assist during the coding phase.
The original ideas and the overall structure are the owner's, and all core logic
has been reviewed. Even so, mistakes or bugs may have slipped past proof-reading —
please report anything unexpected.
