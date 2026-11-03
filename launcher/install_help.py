"""How to install what is missing: the one place the install commands are written.

Defines:    ``install_commands`` (the uv + pip commands for the core app or one extra) and
            ``missing_package_message`` (the full message for one missing module).
Used by:    ``matlas.py`` and ``launcher.gui`` (at start-up), ``gui.app`` (the UFDR dump mode).
Depends on: the standard library only.

WHY it lives here and imports nothing of ours: it must still work when the dependencies are
what is missing, and importing anything under ``model_atlas`` runs the package ``__init__``,
which imports pandas.
"""
from __future__ import annotations

import sys

# Which pyproject extra brings each optional third-party module. A third-party module absent
# from this table comes with the core dependencies.
_EXTRA_OF_MODULE: dict[str, str] = {
    "PySide6": "gui",
    "shiboken6": "gui",
    "lxml": "ufdr",
    "psutil": "ufdr",
}
_FIRST_PARTY_MODULES = frozenset({"gui", "launcher", "matlas", "model_atlas", "tests", "ufdr_parser"})


def install_commands(extra: str | None = None) -> str:
    """The commands installing the core app, or the core app plus ``extra``, one per line.

    ``--inexact`` because a plain ``uv sync`` removes whatever it was not asked for: following
    the message for ``gui`` would silently uninstall ``ufdr`` and ``dev``, and the next missing
    package would be the one this message just removed.
    """
    if extra is None:
        return "  uv sync --inexact\n  python -m pip install ."
    return f"  uv sync --inexact --extra {extra}\n  python -m pip install '.[{extra}]'"


def missing_package_message(module_name: str | None) -> str | None:
    """What to tell the operator about a missing module, or None when no install can fix it.

    None for one of our own modules or a standard-library one: that is a bug or a broken
    Python, and presenting it as a missing package would send the operator to the wrong fix.
    """
    root = (module_name or "").partition(".")[0]
    if not root or root in _FIRST_PARTY_MODULES or root in sys.stdlib_module_names:
        return None
    extra = _EXTRA_OF_MODULE.get(root)
    needs = "the dependencies" if extra is None else f"the '{extra}' extra"
    return (f"{root} is not installed. From the mATLAS folder, install {needs} with one of:\n\n"
            f"{install_commands(extra)}")
