# mATLAS documentation

Every document, grouped by purpose, with what it is for. The root [README](../README.md)
covers only installation and a first run.

## Using mATLAS

| Doc | Read it when |
| --- | --- |
| [guide/running.md](guide/running.md) | You run cases: the append model, every option, outputs, GUI, presets, configuration, profiles. |
| [FAQ.md](FAQ.md) | Something does not behave as expected, or you need a quick answer. |
| [guide/preset_authoring.md](guide/preset_authoring.md) | You need a preset for a new source: a step-by-step walk from a real export to a working preset. |
| [guide/preset_schema.md](guide/preset_schema.md) | You write or review a preset: the full YAML reference (fields, links, conditionals, pipes). |

## Reading the output

| Doc | Read it when |
| --- | --- |
| [reference/csv_contract.md](reference/csv_contract.md) | You work from the flat CSV: each column and where it comes from in the case database. |
| [reference/traceability.md](reference/traceability.md) | You audit a run: the forensic guarantees, the traceability sidecar, known limitations. |
| [reference/conventions.md](reference/conventions.md) | You need the reason behind a processing behaviour, ingest to export. |
| [reference/glossary.md](reference/glossary.md) | A term is used in its specific mATLAS sense. |
| [examples/example_queries.md](examples/example_queries.md) | You query the case database in SQL: worked queries on a demo case. |

## The model

| Doc | Read it when |
| --- | --- |
| [model/conceptual_model.md](model/conceptual_model.md) | You want to know what an observation means: entity, place, time and the links between them. |
| [model/schema.md](model/schema.md) | You read the case database directly: every table, column, constraint and digest. |
| [model/identity_model.md](model/identity_model.md) | You need to know what has an identity, and why nothing is merged automatically. |
| [model/entity_model.md](model/entity_model.md) | You work on identifiers and normalisation, or the deferred analyst graph. |
| [matlas_run_db.xml](matlas_run_db.xml) | You want the schema as a diagram (open it in [WWW SQL Designer](https://sql.toad.cz/)). |

## Extending mATLAS

| Doc | Read it when |
| --- | --- |
| [guide/extending.md](guide/extending.md) | You add an input format (reader) or an output format (exporter). |
| [guide/gui_architecture.md](guide/gui_architecture.md) | You change the GUI: its model–view–controller boundaries. |

## Demo artefacts

[examples/build_example_db.py](examples/build_example_db.py) builds the demo case database
[examples/matlas_example.sqlite](examples/matlas_example.sqlite) and its flat CSV
[examples/matlas_example.csv](examples/matlas_example.csv), used by the example queries and
the test suite.
