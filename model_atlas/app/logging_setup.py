"""Shared logging helpers.

Defines:    LevelAwareFormatter + build_formatter — a formatter that keeps the logger name
            (e.g. ``model_atlas.gui``) on DEBUG lines for precise tracing, but drops it on
            INFO/WARNING/ERROR/CRITICAL so ordinary run output reads cleanly; and
            configure_logging — the root stream/file handler setup shared by entry points.
Used by:    launcher.cli (stream + optional file handlers) and gui.app (the GUI log handler
            reuses build_formatter).
Depends on: standard library only.
"""
from __future__ import annotations

import logging
from pathlib import Path

# The token a format string uses for the logger name. Removing it (with its leading space)
# turns "%(asctime)s %(levelname)s %(name)s: %(message)s" into the same line without the name.
_NAME_TOKEN = " %(name)s"


def _without_name(fmt: str) -> str:
    """The configured format with the logger-name field removed (used for non-DEBUG lines)."""
    return fmt.replace(_NAME_TOKEN, "")


class LevelAwareFormatter(logging.Formatter):
    """Format DEBUG records with the full configured format (logger name included) and every
    higher level without the logger name — so the module/submodule path only clutters the
    verbose debug trace, never the normal INFO run output."""

    def __init__(self, fmt: str) -> None:
        super().__init__(fmt)
        self._debug_formatter = logging.Formatter(fmt)
        self._plain_formatter = logging.Formatter(_without_name(fmt))

    def format(self, record: logging.LogRecord) -> str:
        formatter = self._debug_formatter if record.levelno <= logging.DEBUG else self._plain_formatter
        return formatter.format(record)


def build_formatter(fmt: str) -> logging.Formatter:
    """The application log formatter for ``fmt`` (name shown on DEBUG only)."""
    return LevelAwareFormatter(fmt)


def configure_logging(level_name: str, log_file: Path | None, fmt: str) -> None:
    """Reset the root logger to a stream handler (stderr) plus an optional file handler, both
    using the level-aware formatter. Shared by the CLI entry point; the GUI installs its own Qt
    handler instead but reuses build_formatter for a consistent format."""
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
    level = getattr(logging, level_name.upper(), logging.INFO)
    formatter = build_formatter(fmt)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    root_logger.setLevel(level)
    for handler in handlers:
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
