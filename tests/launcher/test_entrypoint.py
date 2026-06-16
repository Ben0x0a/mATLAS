"""Clone-and-run entry-point routing tests for ``matlas.py``."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


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
