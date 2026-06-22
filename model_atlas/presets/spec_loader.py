"""Recursive loader for declarative (v2) preset specs.

Defines:    load_preset_specs() — read and parse every *.yaml preset under a path,
            skipping template placeholders.
Used by:    the v2 pipeline.
Depends on: presets.spec, pyyaml.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from model_atlas.presets.spec import PresetSpec, preset_spec_from_yaml

log = logging.getLogger(__name__)

_PLACEHOLDER_MARKERS = ("__template__", "TODO")


def _is_template(raw: object) -> bool:
    text = str(raw)
    return any(marker in text for marker in _PLACEHOLDER_MARKERS)


def load_preset_specs(presets_path: Path) -> list[PresetSpec]:
    presets_path = Path(presets_path)
    if not presets_path.exists():
        raise FileNotFoundError(presets_path)
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
