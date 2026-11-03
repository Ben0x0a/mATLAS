"""The install guidance: missing packages (``launcher.install_help`` + ``matlas.py``) and a
missing preset library (``spec_loader`` + the CLI). The GUI's status label is pinned in
tests/gui/test_preset_selection.py.

Used by:    the verification gate (pytest tests/launcher/test_install_help.py).
Depends on: launcher.install_help, launcher.cli, model_atlas.presets.spec_loader, matlas.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from launcher import cli
from launcher.install_help import missing_package_message
from model_atlas.presets.spec_loader import (
    PresetsNotFoundError,
    load_preset_specs,
    missing_presets_message,
)
from tests.launcher.test_entrypoint import _load_entrypoint


@pytest.mark.parametrize("module,extra", [("PySide6", "gui"), ("PySide6.QtGui", "gui"),
                                          ("lxml", "ufdr"), ("psutil", "ufdr")])
def test_an_optional_package_names_its_extra_for_uv_and_pip(module: str, extra: str) -> None:
    message = missing_package_message(module)
    # --inexact: a plain `uv sync --extra gui` would uninstall the other extras.
    assert f"uv sync --inexact --extra {extra}\n" in message
    assert f"python -m pip install '.[{extra}]'" in message


def test_a_core_package_gets_the_plain_install() -> None:
    message = missing_package_message("pandas")
    assert "pandas is not installed" in message
    assert "uv sync --inexact\n" in message and "python -m pip install ." in message
    assert "--extra" not in message


@pytest.mark.parametrize("module", [None, "", "model_atlas.nope", "gui.nope", "sqlite3"])
def test_our_own_or_a_stdlib_module_is_not_an_install_problem(module: str | None) -> None:
    assert missing_package_message(module) is None


def _fail_with(monkeypatch, module_name: str) -> None:
    def _raise(_argv):
        raise ModuleNotFoundError(f"No module named {module_name!r}", name=module_name)
    monkeypatch.setattr("launcher.cli.run", _raise)


def test_entrypoint_turns_a_missing_package_into_the_commands(monkeypatch, capsys) -> None:
    _fail_with(monkeypatch, "pandas")
    assert _load_entrypoint().main(["process"]) == 1
    assert "uv sync" in capsys.readouterr().err


def test_entrypoint_does_not_disguise_a_broken_internal_import(monkeypatch) -> None:
    _fail_with(monkeypatch, "model_atlas.nope")
    with pytest.raises(ModuleNotFoundError):
        _load_entrypoint().main(["process"])


@pytest.mark.parametrize("make", [lambda root: root / "absent", lambda root: root])
def test_missing_or_empty_presets_folder_says_how_to_fetch_it(make, tmp_path: Path) -> None:
    """``presets/`` of an unfetched submodule is empty; ``presets/mATLAS-presets`` is absent."""
    with pytest.raises(PresetsNotFoundError) as caught:
        load_preset_specs(make(tmp_path))
    message = str(caught.value)
    assert str(make(tmp_path)) in message and "git submodule update --init" in message
    # One line: the GUI shows only the last line of a failed run's traceback.
    assert "\n" not in message


def test_a_folder_holding_a_preset_is_not_missing(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "p.yaml").write_text("preset: {}\n", encoding="utf-8")
    assert missing_presets_message(tmp_path) is None


def test_cli_reports_missing_presets_without_a_traceback(tmp_path: Path, capsys) -> None:
    code = cli.run(["process", "--input", str(tmp_path), "--presets",
                    str(tmp_path / "absent"), "--output", str(tmp_path / "out"),
                    "--concerns", "Subject"])
    err = capsys.readouterr().err
    assert code == 1
    assert "git submodule update --init" in err and "Traceback" not in err
