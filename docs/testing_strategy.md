# Testing Strategy

Required test groups:

- Package imports and public API routing.
- Recursive preset loading and v2 schema validation.
- Source discovery and adapter extraction for CSV, Excel, SQLite, ZIP, and
  folders.
- Preset matching and unmatched-source reporting.
- Expected-column drift warnings and mapped-column behavior.
- `common`, `assertions`, temporal expansion, pipes, source row IDs, and
  provenance defaults.
- Merge output and split per-preset output.
- Traceability and warnings sidecars.
- Untangle grouping and ranking.
- CLI `python matlas.py process` behavior.
- Launcher routing for no-argument GUI launch, explicit `gui`, and CLI commands.
- GUI-only helpers where they affect launchability, accessibility, profiles, or
  output mode wiring.

## Test Layout

| Path | Scope |
| --- | --- |
| `tests/source_agnostic/` | Package pipeline, source-agnostic CLI behavior, presets, AXIOM parser behavior. |
| `tests/launcher/` | Top-level launcher routing and CLI-specific behavior. |
| `tests/presets/` | Preset parsing and matching. |
| `tests/sources/` | Source discovery and adapter registry behavior. |
| `tests/transforms/` | Row assembly, pipes, value mapping, and untangle. |

## Verification Command

```bash
.venv/bin/python -m pytest
```
