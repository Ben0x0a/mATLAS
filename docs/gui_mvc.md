# GUI MVC Design

The GUI is planned after the core + CLI slice.

Rules:

- Views contain only widgets and layout.
- Controllers handle user interaction.
- Workers call the public `model_atlas` package interface.
- No preset parsing, source extraction, mapping, export, or traceability logic
  lives in GUI files.

The GUI should expose the same workflow as the CLI: source selection, preset
matching, warnings, preview for supported single elements, and run-all.
