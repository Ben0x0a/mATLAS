# Model Atlas Documentation

This directory is the audit-oriented documentation set. It explains not
only *how to use* the tool but also *how it works*, where the main trust
boundaries are, and which files to review for any given behaviour.

The top-level [README](../README.md) covers installation and quick-start
usage. The documents below go deeper.

## Documents

| Document | Covers |
| --- | --- |
| [Architecture](architecture.md) | Module responsibilities, dependency direction, CLI/GUI boundaries. |
| [Integration Model](integration_model.md) | Canonical output columns, types, units, and temporal relation semantics. |
| [Preset Quickstart](preset_authoring_quickstart.md) | Practical v2 preset authoring path for real source data. |
| [Preset Schema](preset_schema.md) | Current YAML fields, mapping syntax, pipes, and expansion logic. |
| [Transform Behavior](transform_behavior.md) | Source-agnostic row assembly, merge/split output, and untangle behavior. |
| [GUI MVC](gui_mvc.md) | GUI architecture rules and current PySide6 front-end boundaries. |
| [Source Adapters](source_adapters.md) | CSV, Excel, SQLite, ZIP, and folder discovery behavior. |
| [Traceability](traceability.md) | Forensic guarantees, traceability JSON schema, limitations. |

## Where to start, by role

- **Operator**: start with the top-level [README](../README.md), then [preset_authoring_quickstart.md](preset_authoring_quickstart.md).
- **Auditor**: start with [traceability.md](traceability.md), then [integration_model.md](integration_model.md).
- **Developer**: start with [architecture.md](architecture.md), then the specific schema/source/transform docs.

## Verification command

Run the full test suite with the project virtual environment:

```bash
.venv/bin/python -m pytest
```
