# AXIOM Model Parser

Utility for parsing Magnet AXIOM artifact reference `.htm` files and generating
starter source-agnostic preset YAML files.

The parser reads either a folder of `.htm` files or a ZIP containing `.htm`
files. The HTML filename is treated as the CSV name and Excel sheet name.

Example:

```bash
.venv/bin/python utils/AXIOM_model_parser/axiom_model_parser.py \
  "/Volumes/zse20302/Test AXIOM/Model/Android - Location and Travel.zip" \
  --platform Android
```

By default the script writes into:

```text
utils/AXIOM_model_parser/results/<platform>/<input-name>/
```

It also compares generated parser IDs against presets found under `presets/`
and prints:

- missing presets;
- presets whose `expected_columns` differ;
- unchanged presets.

Use `--no-write` to only print the comparison report.

Generated YAML files are templates. AXIOM attribute descriptions are written as
comments at the top of each file, while the actual YAML data contains only the
preset fields used by the app. The best-effort mappings still need review
against real source files. Review and copy completed presets into `presets/`
manually; the utility results folder is safe to regenerate.
