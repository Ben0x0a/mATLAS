"""Clone-and-run entry-point routing tests for ``matlas.py``."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace


def _load_entrypoint() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "matlas.py"
    spec = importlib.util.spec_from_file_location("matlas_entrypoint", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_args_launches_gui(monkeypatch):
    entrypoint = _load_entrypoint()
    captured = {}

    def fake_gui_run(argv):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr("launcher.gui.run", fake_gui_run)

    assert entrypoint.main([]) == 0
    assert captured == {"argv": []}


def test_cli_args_route_to_cli(monkeypatch):
    entrypoint = _load_entrypoint()
    captured = {}

    def fake_cli_run(argv):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr("launcher.cli.run", fake_cli_run)

    assert entrypoint.main(["process", "--help"]) == 0
    assert captured == {"argv": ["process", "--help"]}


def test_profile_run_forwards_root_prefix_depth(monkeypatch, tmp_path):
    """--root-prefix-depth must reach process() even under --profile (regression: the
    profile branch used to omit it, silently ignoring the flag)."""
    import launcher.cli as cli

    captured = {}

    def fake_process(_input, _presets, _output, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            row_counts={}, matched=[], unmatched=[], warnings=[],
            output_case_db=Path(_output) / "merged.matlas.sqlite",
            output_csv=None, output_traceability=None,
            output_warnings=None, output_extras=[],
        )

    monkeypatch.setattr(cli, "process", fake_process)

    preset = tmp_path / "p.yaml"
    preset.write_text("selectors: []\n", encoding="utf-8")
    profile = tmp_path / "prof.mATLAS-profile"
    profile.write_text(
        json.dumps({"version": 1, "preset_paths": [str(preset)]}), encoding="utf-8"
    )
    source = tmp_path / "in.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")

    rc = cli.run([
        "process",
        "--input", str(source),
        "--output", str(tmp_path / "out.csv"),
        "--linked-entity", "subject",
        "--profile", str(profile),
        "--root-prefix-depth", "3",
    ])

    assert rc == 0
    assert captured["root_prefix_depth"] == 3


def test_root_prefix_depth_unset_falls_back_to_config(monkeypatch, tmp_path):
    """With no --root-prefix-depth flag the CLI forwards None, so process() falls back to the
    matlas_config.toml value — identical to the GUI (which never sends the arg). Previously the
    argparse default of 1 was ALWAYS forwarded, overriding an operator's config on the CLI."""
    import launcher.cli as cli

    captured = {}

    def fake_process(_input, _presets, _output, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            row_counts={}, matched=[], unmatched=[], warnings=[],
            output_case_db=Path(_output) / "merged.matlas.sqlite",
            output_csv=None, output_traceability=None,
            output_warnings=None, output_extras=[],
        )

    monkeypatch.setattr(cli, "process", fake_process)

    source = tmp_path / "in.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")

    rc = cli.run([
        "process",
        "--input", str(source),
        "--output", str(tmp_path / "out"),
        "--linked-entity", "subject",
    ])

    assert rc == 0
    # None is the "fall back to config" sentinel process() already honours (not the old 1).
    assert captured["root_prefix_depth"] is None
