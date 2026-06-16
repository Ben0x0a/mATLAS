"""CLI/GUI profile files for selected preset lists.

Profiles intentionally live in launcher infrastructure, not ``model_atlas``:
they are a convenience for frontends to pass a curated preset set into the
existing package API.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

PROFILE_SUFFIX = ".mATLAS-profile"
PROFILE_VERSION = 1


def load_profile(path: Path) -> list[Path]:
    """Return preset paths stored in a ``*.mATLAS-profile`` file."""
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: profile must be a JSON object")
    version = raw.get("version")
    if version != PROFILE_VERSION:
        raise ValueError(f"{path}: unsupported profile version {version!r}")
    presets = raw.get("preset_paths")
    if not isinstance(presets, list) or not all(isinstance(item, str) for item in presets):
        raise ValueError(f"{path}: preset_paths must be a list of strings")
    base = path.parent
    return [
        (base / item).resolve() if not Path(item).is_absolute() else Path(item)
        for item in presets
    ]


def save_profile(path: Path, preset_paths: list[Path]) -> Path:
    """Write a selected-preset profile."""
    path = Path(path)
    if path.suffix != PROFILE_SUFFIX:
        path = path.with_suffix(PROFILE_SUFFIX)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": PROFILE_VERSION,
        "preset_paths": [str(Path(item).resolve()) for item in preset_paths],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def build_profile_preset_folder(preset_paths: list[Path], target_dir: Path) -> Path:
    """Copy profile presets into a folder accepted by ``process(..., presets_path)``."""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    for index, path in enumerate(preset_paths, start=1):
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        suffix = path.suffix or ".yaml"
        shutil.copy2(path, target_dir / f"{index:03d}-{path.stem}{suffix}")
    return target_dir
