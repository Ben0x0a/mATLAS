"""Recursive loader for declarative (v3) preset specs.

Defines:    load_preset_specs() — read and parse every *.yaml preset under a path,
            skipping template placeholders; PresetsNotFoundError and
            missing_presets_message() — what to tell the operator when there is nothing to
            read, which on a fresh clone means the presets submodule was not fetched.
Used by:    the v3 pipeline, launcher.cli and gui.app (the two messages).
Depends on: presets.spec, pyyaml.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from model_atlas.presets.spec import PresetSpec, preset_spec_from_yaml

log = logging.getLogger(__name__)

_PLACEHOLDER_MARKERS = ("__template__", "TODO")
# Consumed by missing_presets_message() below and by gui.app's short status label.
PRESETS_FETCH_COMMAND = "git submodule update --init"
_PRESETS_REPOSITORY_URL = "https://github.com/Ben0x0a/mATLAS-presets.git"


class PresetsNotFoundError(FileNotFoundError):
    """No preset file at the given path; the message says how to fetch the library."""


def missing_presets_message(presets_path: Path) -> str | None:
    """Why ``presets_path`` holds no preset file and how to fix it, or None when it holds one.

    ONE line on purpose: the GUI shows the last line of a failed run's traceback, so a
    multi-line message would lose its beginning there. A folder with no YAML at all is the
    same fault as a missing one: an unfetched submodule leaves ``presets/`` empty.
    """
    presets_path = Path(presets_path)
    if presets_path.is_file() or next(presets_path.rglob("*.yaml"), None) is not None:
        return None
    return (f"Presets not found at {presets_path}. The preset library is the presets/ git "
            f"submodule: from the mATLAS folder run '{PRESETS_FETCH_COMMAND}' (without a git "
            f"clone of mATLAS: 'git clone {_PRESETS_REPOSITORY_URL} presets'), then use "
            f"presets/mATLAS-presets.")


def _is_template(raw: object) -> bool:
    text = str(raw)
    return any(marker in text for marker in _PLACEHOLDER_MARKERS)


def load_preset_specs(presets_path: Path) -> list[PresetSpec]:
    presets_path = Path(presets_path)
    missing = missing_presets_message(presets_path)
    if missing is not None:
        raise PresetsNotFoundError(missing)
    files = [presets_path] if presets_path.is_file() else sorted(presets_path.rglob("*.yaml"))
    specs: list[PresetSpec] = []
    for path in files:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if _is_template(raw):
            # A template is documentation, not a live preset — never load it for matching.
            log.debug(f"Skipping template/placeholder preset: {path}")
            continue
        specs.append(preset_spec_from_yaml(raw, path))
    if not specs:
        raise ValueError(f"No declarative presets found under {presets_path}")
    log.info(f"Loaded {len(specs)} declarative preset(s) from {presets_path}")
    return specs
