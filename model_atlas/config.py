"""Application settings loaded from an optional ``matlas_config.toml`` in the CWD.

Defines:    the typed Settings tree (timezone / logging / discovery / output) and
            load_settings() / get_settings() — preferences that change rarely, not per run.
Used by:    the launcher (logging), the pipeline (discovery depths + local timezone), and
            the transform layer (via BuildEnv.local_zone).
Depends on: stdlib only (tomllib, zoneinfo).

Design:
- **TOML**, read with stdlib ``tomllib`` (Python >= 3.11) — the idiomatic Python config
  format (same as pyproject.toml), no new dependency.
- A single ``./matlas_config.toml`` (CWD). **Missing is not an error** — built-in defaults
  are used so library/package use just works; only an INFO line notes it. A missing
  ``local_zone`` means the source's local zone is unknown -> ``utc_offset_hours`` is null.
- A present-but-invalid value (a bad IANA zone, an unknown key) IS an error: a config typo
  must fail fast, never be silently ignored.
- Security **safeguards** (zip-bomb guards) are deliberately NOT here; they stay as code
  defaults so they cannot be casually weakened from a working-directory file.
"""
from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

CONFIG_FILENAME = "matlas_config.toml"

_DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# The known schema; an unknown section or key is a config typo we flag rather than ignore.
_SCHEMA: dict[str, set[str]] = {
    "timezone": {"local_zone"},
    "logging": {"level", "format"},
    "discovery": {"root_prefix_depth", "max_container_depth"},
    "output": {"source_column_prefix"},
}


@dataclass(frozen=True)
class TimezoneSettings:
    # IANA name (e.g. "Europe/Paris"). None => unknown -> utc_offset_hours is null.
    local_zone: str | None = None


@dataclass(frozen=True)
class LoggingSettings:
    level: str = "INFO"
    format: str = _DEFAULT_LOG_FORMAT


@dataclass(frozen=True)
class DiscoverySettings:
    root_prefix_depth: int = 1
    max_container_depth: int = 1


@dataclass(frozen=True)
class OutputSettings:
    source_column_prefix: str = "orig_"


@dataclass(frozen=True)
class Settings:
    timezone: TimezoneSettings = field(default_factory=TimezoneSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    discovery: DiscoverySettings = field(default_factory=DiscoverySettings)
    output: OutputSettings = field(default_factory=OutputSettings)
    source_path: Path | None = None     # where it was loaded from; None => built-in defaults

    def to_traceability(self) -> dict:
        """A flat record of the resolved settings, for the run's traceability."""
        return {
            "config_file": str(self.source_path) if self.source_path else None,
            "local_zone": self.timezone.local_zone,
            "root_prefix_depth": self.discovery.root_prefix_depth,
            "max_container_depth": self.discovery.max_container_depth,
            "source_column_prefix": self.output.source_column_prefix,
        }


def _reject_unknown(raw: dict) -> None:
    for section, keys in raw.items():
        if section not in _SCHEMA:
            raise ValueError(f"{CONFIG_FILENAME}: unknown section [{section}]; expected one of {sorted(_SCHEMA)}")
        if not isinstance(keys, dict):
            raise ValueError(f"{CONFIG_FILENAME}: section [{section}] must be a table")
        unknown = set(keys) - _SCHEMA[section]
        if unknown:
            raise ValueError(f"{CONFIG_FILENAME}: unknown key(s) {sorted(unknown)} in [{section}]")


def _validate_zone(name: str | None) -> str | None:
    if name is None:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"{CONFIG_FILENAME}: [timezone].local_zone {name!r} is not a valid IANA zone "
            f"(e.g. 'Europe/Paris', 'UTC')"
        ) from exc
    return name


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from ``path`` (or ``./matlas_config.toml``). Missing -> defaults."""
    config_path = Path(path) if path is not None else Path.cwd() / CONFIG_FILENAME
    if not config_path.is_file():
        log.info(
            "No %s in %s; using built-in defaults (local timezone unknown -> utc_offset_hours null)",
            CONFIG_FILENAME, config_path.parent,
        )
        return Settings()
    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)
    _reject_unknown(raw)
    tz = raw.get("timezone", {})
    lg = raw.get("logging", {})
    dc = raw.get("discovery", {})
    out = raw.get("output", {})
    settings = Settings(
        timezone=TimezoneSettings(local_zone=_validate_zone(tz.get("local_zone"))),
        logging=LoggingSettings(
            level=str(lg.get("level", "INFO")),
            format=str(lg.get("format", _DEFAULT_LOG_FORMAT)),
        ),
        discovery=DiscoverySettings(
            root_prefix_depth=int(dc.get("root_prefix_depth", 1)),
            max_container_depth=int(dc.get("max_container_depth", 1)),
        ),
        output=OutputSettings(source_column_prefix=str(out.get("source_column_prefix", "orig_"))),
        source_path=config_path,
    )
    log.info("Loaded settings from %s (local_zone=%s)", config_path, settings.timezone.local_zone)
    return settings


_CACHED: Settings | None = None


def get_settings() -> Settings:
    """The process-wide settings, loaded once from ``./matlas_config.toml`` (or defaults)."""
    global _CACHED
    if _CACHED is None:
        _CACHED = load_settings()
    return _CACHED


def set_settings(settings: Settings) -> None:
    """Override the cached settings (tests / explicit programmatic configuration)."""
    global _CACHED
    _CACHED = settings


def reset_settings() -> None:
    """Drop the cache so the next get_settings() reloads (tests)."""
    global _CACHED
    _CACHED = None
