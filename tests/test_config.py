"""Tests for the matlas_config.toml loader (model_atlas.config)."""
from __future__ import annotations

from pathlib import Path

import pytest

from model_atlas.config import CONFIG_FILENAME, Settings, load_settings


def test_missing_config_uses_defaults_no_error(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / CONFIG_FILENAME)
    assert isinstance(settings, Settings)
    assert settings.timezone.local_zone is None          # absence => unknown zone
    assert settings.discovery.root_prefix_depth == 1
    assert settings.output.source_column_prefix == "orig_"
    assert settings.source_path is None


def test_loads_values(tmp_path: Path) -> None:
    cfg = tmp_path / CONFIG_FILENAME
    cfg.write_text(
        '[timezone]\nlocal_zone = "Europe/Paris"\n'
        '[logging]\nlevel = "DEBUG"\n'
        '[discovery]\nroot_prefix_depth = 2\nmax_container_depth = 3\n'
        '[output]\nsource_column_prefix = "src_"\n',
        encoding="utf-8",
    )
    s = load_settings(cfg)
    assert s.timezone.local_zone == "Europe/Paris"
    assert s.logging.level == "DEBUG"
    assert s.discovery.root_prefix_depth == 2
    assert s.discovery.max_container_depth == 3
    assert s.output.source_column_prefix == "src_"
    assert s.source_path == cfg
    assert s.to_traceability()["local_zone"] == "Europe/Paris"


def test_invalid_zone_is_a_hard_error(tmp_path: Path) -> None:
    cfg = tmp_path / CONFIG_FILENAME
    cfg.write_text('[timezone]\nlocal_zone = "Mars/Olympus"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="not a valid IANA zone"):
        load_settings(cfg)


def test_unknown_section_is_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / CONFIG_FILENAME
    cfg.write_text('[bogus]\nx = 1\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown section"):
        load_settings(cfg)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / CONFIG_FILENAME
    cfg.write_text('[timezone]\nlocal_zone = "UTC"\ntypo = 1\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown key"):
        load_settings(cfg)
