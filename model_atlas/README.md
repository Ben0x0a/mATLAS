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
    output_csv=Path("out/merged.csv"),
)
print(result.row_counts)
```

`process()` discovers sources under `input_path`, matches them to YAML presets,
extracts and maps columns into the 40-column assertion model, runs untangle, and
writes `output_csv` plus `.matlas.traceability.json` and `.matlas.warnings.json`
sidecars. It returns a `ProcessResult` with row counts, matched/unmatched source
lists, and warning strings.

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
