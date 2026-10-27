# Model Atlas

Model Atlas (mATLAS) turns heterogeneous forensic exports — CSV, Excel, SQLite and XML, loose
or inside ZIP acquisitions — into one spatio-temporal **case database**: every record that says
an entity was (or was not) somewhere at some time becomes an *observation*, kept verbatim and
traced to its source. The flat CSV, KML, GeoJSON and Excel outputs are projections of that
database.

## Requirements

- Python 3.11 to 3.14
- Git — the preset library ([mATLAS-presets](https://github.com/Ben0x0a/mATLAS-presets)) is
  a separate repository, included here as the `presets/` submodule

## Installation

```bash
git clone --recurse-submodules https://github.com/Ben0x0a/mATLAS.git
cd mATLAS
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install .            # add '.[gui]' for the graphical interface
```

## Usage

```bash
python matlas.py process \
  --input ./evidence \
  --presets presets/mATLAS-presets \
  --output ./out \
  --name operation-falcon \
  --concerns "Case Subject"
```

| Argument | Required | Meaning |
| --- | --- | --- |
| `--input` | yes | Evidence file or folder (ZIP acquisitions included). |
| `--presets` | yes¹ | Preset YAML file or folder. |
| `--profile` | | A `.mATLAS-profile` listing presets; replaces `--presets`. |
| `--concerns` | yes | The case subject every observation is attributed to. |
| `--entity` | | The entity every row is about (overrides the preset). |
| `--output` | yes | Output folder. |
| `--name` | | File stem of the case database and outputs (default `merged`). |
| `--case` | | Human case label, stored when the case is created. |
| `--export` | | Extra output: `kml`, `geojson`, `xlsx`, `geovisualizer`, `sqlite_db` (repeatable). |
| `--no-source-columns` | | Omit the verbatim `orig_*` source columns from the CSV. |

¹ unless `--profile` is given. Run `python matlas.py process --help` for every option, and
`python matlas.py --log-level DEBUG process …` for a step-by-step trace.

Running again with the same `--output` and `--name` **appends** to the existing case. Outputs:
`<name>.matlas.sqlite` (the case database), `<name>.csv`, and the traceability and warning
sidecars. The graphical interface starts with `python matlas.py`.

## Documentation

- [docs/README.md](docs/README.md) — every document and when to use it
- [docs/guide/running.md](docs/guide/running.md) — the full operating guide
- [docs/FAQ.md](docs/FAQ.md) — common questions and fixes

## Development & AI use

Generative AI was used in this project mainly to assist during the coding phase. The original
ideas and the overall structure are the owner's, and all core logic has been reviewed. Even so,
mistakes or bugs may have slipped past proof-reading — please report anything unexpected.

## License

Apache-2.0 — see [LICENSE](LICENSE).
