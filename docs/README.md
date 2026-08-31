# mATLAS documentation

The reference set for mATLAS: what it records, how to author presets and extend it, and the
conventions its output follows. The docs are grouped by purpose. The top-level
[README](../README.md) covers installation and quick-start usage; the documents below go deeper.

## The model

The settled data model — what the run database records and why.

| Doc | What it covers |
| --- | --- |
| [model/conceptual_model.md](model/conceptual_model.md) | What mATLAS records at the conceptual level: the entity–space–time observation. |
| [model/schema.md](model/schema.md) | The run database schema — the tables, their columns, and the layer discipline. |
| [model/entity_model.md](model/entity_model.md) | The entity side: identifiers, normalisation, and the deferred analyst layer. |
| [model/identity_model.md](model/identity_model.md) | What carries an identity in the model, and why. |

## Guides

How to use and extend mATLAS.

| Doc | What it covers |
| --- | --- |
| [guide/preset_authoring.md](guide/preset_authoring.md) | Turn one real source into a working preset, step by step. |
| [guide/preset_schema.md](guide/preset_schema.md) | The full preset YAML reference: fields, mapping syntax, pipes, expansion. |
| [guide/extending.md](guide/extending.md) | Add a reader (input format) or an exporter (output format). |
| [guide/gui_architecture.md](guide/gui_architecture.md) | The GUI's model–view–controller structure and boundaries. |

## Reference

Look up how the output behaves.

| Doc | What it covers |
| --- | --- |
| [reference/conventions.md](reference/conventions.md) | Every deliberate processing convention, ingest to export, with the reason for each. |
| [reference/glossary.md](reference/glossary.md) | The project's terms, each in its specific mATLAS sense. |
| [reference/csv_contract.md](reference/csv_contract.md) | The flat-CSV column contract: the exact projection over the run database. |
| [reference/traceability.md](reference/traceability.md) | Forensic guarantees, the traceability sidecar, and known limitations. |

## Examples

| Doc | What it covers |
| --- | --- |
| [examples/example_queries.md](examples/example_queries.md) | Worked SQL queries against a demo case database. |

## Where to start

- **Operator** — the top-level [README](../README.md), then [guide/preset_authoring.md](guide/preset_authoring.md).
- **Auditor** — [reference/traceability.md](reference/traceability.md), then [reference/conventions.md](reference/conventions.md).
- **Contributor** — [model/schema.md](model/schema.md), then [guide/extending.md](guide/extending.md).

## Verification command

```bash
.venv/bin/python -m pytest
```
