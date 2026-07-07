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
    "diagnostics": {
        "enabled", "crash_dir", "redact", "redact_paths", "max_string_len", "max_items",
        "max_head", "max_depth", "max_frame_bytes", "capture_env",
    },
    "export": {"plugins_dir"},
}

# Variable-name tokens whose value is redacted (case-insensitive substring) in a crash
# report. Deliberately specific: bare "key" would over-redact legitimate forensic columns
# (primary_key, keyword, ...), so only unambiguously sensitive tokens are defaulted here.
# Consumed by: diagnostics._is_redacted. Operator-overridable via [diagnostics].redact.
_DEFAULT_REDACT: tuple[str, ...] = (
    "password", "passwd", "pwd", "passphrase",
    "secret", "token", "credential", "api_key", "apikey", "private_key",
)


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
class DiagnosticsSettings:
    """Crash-report behaviour. Consumed by ``model_atlas.diagnostics``.

    ``redact`` is the case-insensitive substring token list applied to VARIABLE NAMES; a
    matched variable's value is replaced by a type + sha256 (never the plaintext). The size
    caps keep a report useful without ever flushing a whole DataFrame/collection to disk.
    """

    enabled: bool = True
    crash_dir: str = "crash_reports"     # relative => resolved against the CWD at crash time
    redact: tuple[str, ...] = _DEFAULT_REDACT
    redact_paths: bool = True            # anonymise filesystem paths (username / org / evidence dirs)
    max_string_len: int = 512            # str/repr preview length before hashing the remainder
    max_items: int = 20                  # collection items summarised per container
    max_head: int = 3                    # DataFrame/Series head rows captured (never the whole frame)
    max_depth: int = 3                   # recursion depth before a value degrades to a shallow repr
    max_frame_bytes: int = 65_536        # per-frame locals budget; further locals are dropped past it
    capture_env: bool = False            # include os.environ (redacted) — off by default (holds secrets)


@dataclass(frozen=True)
class ExportSettings:
    # Folder scanned at runtime for drop-in exporter .py files (see model_atlas.exporters).
    # Relative => resolved against the directory you run from. Missing folder => builtins only.
    plugins_dir: str = "matlas_exports"


@dataclass(frozen=True)
class Settings:
    timezone: TimezoneSettings = field(default_factory=TimezoneSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    discovery: DiscoverySettings = field(default_factory=DiscoverySettings)
    output: OutputSettings = field(default_factory=OutputSettings)
    diagnostics: DiagnosticsSettings = field(default_factory=DiagnosticsSettings)
    export: ExportSettings = field(default_factory=ExportSettings)
    source_path: Path | None = None     # where it was loaded from; None => built-in defaults

    def to_traceability(self) -> dict:
        """A flat record of the resolved settings, for the run's traceability."""
        return {
            "config_file": str(self.source_path) if self.source_path else None,
            "local_zone": self.timezone.local_zone,
            "root_prefix_depth": self.discovery.root_prefix_depth,
            "max_container_depth": self.discovery.max_container_depth,
            "source_column_prefix": self.output.source_column_prefix,
            "diagnostics_enabled": self.diagnostics.enabled,
            "crash_dir": self.diagnostics.crash_dir,
            "export_plugins_dir": self.export.plugins_dir,
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


def _load_diagnostics(raw: dict) -> DiagnosticsSettings:
    """Parse the optional ``[diagnostics]`` table into a DiagnosticsSettings.

    ``redact``, if present, MUST be a list of strings — a scalar there is a config typo we
    fail fast on rather than silently coercing (an unredacted secret is worse than an error).
    """
    redact_raw = raw.get("redact")
    if redact_raw is None:
        redact = _DEFAULT_REDACT
    else:
        if not isinstance(redact_raw, list) or not all(isinstance(t, str) for t in redact_raw):
            raise ValueError(f"{CONFIG_FILENAME}: [diagnostics].redact must be a list of strings")
        redact = tuple(redact_raw)
    return DiagnosticsSettings(
        enabled=bool(raw.get("enabled", True)),
        crash_dir=str(raw.get("crash_dir", "crash_reports")),
        redact=redact,
        redact_paths=bool(raw.get("redact_paths", True)),
        max_string_len=int(raw.get("max_string_len", 512)),
        max_items=int(raw.get("max_items", 20)),
        max_head=int(raw.get("max_head", 3)),
        max_depth=int(raw.get("max_depth", 3)),
        max_frame_bytes=int(raw.get("max_frame_bytes", 65_536)),
        capture_env=bool(raw.get("capture_env", False)),
    )


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
    dg = raw.get("diagnostics", {})
    ex = raw.get("export", {})
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
        diagnostics=_load_diagnostics(dg),
        export=ExportSettings(plugins_dir=str(ex.get("plugins_dir", "matlas_exports"))),
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
