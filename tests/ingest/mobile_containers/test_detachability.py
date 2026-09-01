"""The invariant that makes ``mobile_containers`` liftable: it imports nothing but stdlib.

Defines:    a static check, over every module in the package, that no import reaches
            ``model_atlas`` or any third-party distribution.
Used by:    the test suite.
Depends on: ``ast`` / ``sys.stdlib_module_names`` and the package's source files — NOT the
            package's runtime behaviour, so the check holds even for a module that a
            given test run never imports.

WHY a static check rather than an import-time one: an import guarded by a ``TYPE_CHECKING``
block, or living in a module no test happens to load, would slip past a runtime probe and
silently re-couple the package to mATLAS. Parsing every file catches all of them.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import model_atlas.ingest.utils.mobile_containers as mobile_containers

PACKAGE_ROOT = Path(mobile_containers.__file__).parent
FORBIDDEN_ROOT = "model_atlas"


def _sources() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _absolute_import_roots(tree: ast.AST) -> set[str]:
    """The top-level module name of every ABSOLUTE import in a parsed module.

    Explicit relative imports (``level > 0``) are intra-package by construction and are
    therefore not reported — they are the very mechanism that keeps the package movable.
    """
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_package_has_sources():
    """Guard the guard: an empty file list would make every assertion below vacuous."""
    assert len(_sources()) >= 8


@pytest.mark.parametrize("source", _sources(), ids=lambda p: p.name)
def test_no_model_atlas_import(source: Path):
    """No module may import mATLAS — that coupling is what detaching would have to undo."""
    roots = _absolute_import_roots(ast.parse(source.read_text(encoding="utf-8")))
    assert FORBIDDEN_ROOT not in roots, (
        f"{source} imports {FORBIDDEN_ROOT}; the package must stay liftable by a git mv"
    )


@pytest.mark.parametrize("source", _sources(), ids=lambda p: p.name)
def test_standard_library_only(source: Path):
    """No third-party dependency either — a lifted package must install with no extras."""
    roots = _absolute_import_roots(ast.parse(source.read_text(encoding="utf-8")))
    outside = sorted(root for root in roots if root not in sys.stdlib_module_names)
    assert not outside, f"{source} imports non-stdlib module(s): {outside}"
