# GUI MVC Design

The PySide6 GUI is a thin launcher/front-end for the same package pipeline used
by the CLI.

Rules:

- Views live in `.ui` files and contain widgets/layout only.
- Controller code handles user interaction and GUI-only state.
- Workers call public package APIs.
- No preset parsing, source extraction, mapping, export, or traceability logic
  lives in GUI files.

Module layout (`gui/`):

- `app.py` — `MainController`, the sole controller: builds the window from
  `main_window.ui`, binds widgets, wires signals, and dispatches a pipeline run
  onto a background thread. It holds no worker-thread or SVG-rendering machinery.
- `worker.py` — background-thread plumbing. `TaskRunner(parent)` owns the
  `QThread`/worker lifecycle (`run(fn, on_finished, on_failed)` / `wait_all()`);
  `install_gui_log_handler(bridge_parent, on_record)` attaches the coloured GUI log
  handler to the root logger; `ProgressBridge` marshals per-phase progress onto the
  GUI thread. The log-level→colour map lives with the handler here.
- `header_assets.py` — packaged image assets and SVG→pixmap rendering:
  `asset_path`, `DESKTOP_ICON_ASSET`/`TRANSITION_ASSETS`, and
  `configure_header_assets(window)` for the header image row.
- `theme.py`, `accessibility.py`, `completion.py`,
  `widgets/drag_drop_line_edit.py` — supporting concerns split out the same way.

The controller runs a pipeline through a single `process()` call site: preset mode
(force-preset / auto / selected) differs only in how the presets path is resolved
by `MainController._resolve_presets_path`, which uses an `ExitStack` to own any
temporary preset folder for the duration of the run.

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
