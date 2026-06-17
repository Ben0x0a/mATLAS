# GUI MVC Design

The PySide6 GUI is a thin launcher/front-end for the same package pipeline used
by the CLI.

Rules:

- Views live in `.ui` files and contain widgets/layout only.
- Controller code handles user interaction and GUI-only state.
- Workers call public package APIs.
- No preset parsing, source extraction, mapping, export, or traceability logic
  lives in GUI files.

Current GUI workflow:

- Source file/folder selection.
- Preset root selection from a folder or YAML file.
- Auto preset matching or manually selected presets.
- Profile load/save for curated preset sets.
- Merge output or per-preset split output.
- Traceability format and log-level controls.
- Run, open output folder, clear form, and clear log actions.

Profiles intentionally live in `launcher/`, not `model_atlas`, because they are
a CLI/GUI convenience for passing a curated preset list into the existing
package API.

The GUI can be launched with:

```bash
python matlas.py
python matlas.py gui
```

The installed script entry point is:

```bash
matlas-gui
```
