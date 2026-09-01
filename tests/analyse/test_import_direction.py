"""The layout rule, asserted: no analysis may import another analysis.

Uses:       ``ast`` over the ``model_atlas.analyse`` package tree — no imports of the
            analyses themselves, so a broken analysis cannot mask a broken rule.
Depends on: the standard library only.

WHY this test exists: the one-package-per-analysis layout is only worth the churn if it
holds. It did not hold before: ``case_vocabulary`` imported ``_FROM_JOINS`` from the
concomitance engine, so "which tokens exist in this case" depended on the concomitance
engine being importable. A convention nothing checks rots back to that within a release,
so the convention is a test.

The rule: a module under ``analyse/<one>/`` may import ``analyse.helpers``,
``model_atlas.core``, other layers and the standard library — never ``analyse/<other>/``.
``helpers`` itself may import no analysis at all (that would invert the dependency).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ANALYSE_ROOT = Path(__file__).resolve().parents[2] / "model_atlas" / "analyse"
HELPERS = "helpers"
PREFIX = "model_atlas.analyse."


def _packages() -> list[str]:
    """The analysis package names (every sub-package of ``analyse``, helpers included)."""
    return sorted(p.name for p in ANALYSE_ROOT.iterdir()
                  if p.is_dir() and (p / "__init__.py").exists())


def _imported_analyse_packages(module: Path) -> set[str]:
    """Every ``model_atlas.analyse.<pkg>`` this module imports, by package name.

    HOW: parse, then read both ``import a.b`` and ``from a.b import c`` forms. Relative
    imports are resolved against the module's own package, since ``from . import x`` inside
    an analysis is self-reference, not a cross-import.
    """
    found: set[str] = set()
    tree = ast.parse(module.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module] if (node.level == 0 and node.module) else []
        else:
            continue
        for name in names:
            if name.startswith(PREFIX):
                found.add(name[len(PREFIX):].split(".")[0])
    return found


def _modules() -> list[tuple[str, Path]]:
    return [(pkg, path)
            for pkg in _packages()
            for path in sorted((ANALYSE_ROOT / pkg).rglob("*.py"))]


def test_the_layout_has_a_helpers_package_and_at_least_two_analyses() -> None:
    packages = _packages()
    assert HELPERS in packages, f"no helpers package in {packages}"
    assert len(packages) >= 3, f"expected helpers + >=2 analyses, got {packages}"


@pytest.mark.parametrize("pkg,module", _modules(), ids=lambda v: getattr(v, "name", v))
def test_no_analysis_imports_another_analysis(pkg: str, module: Path) -> None:
    """An analysis imports ``helpers`` or itself — never a sibling analysis."""
    allowed = {pkg, HELPERS}
    offending = _imported_analyse_packages(module) - allowed
    assert not offending, (
        f"{module.relative_to(ANALYSE_ROOT.parent.parent)} imports analyse "
        f"package(s) {sorted(offending)}. An analysis may import only analyse.helpers "
        f"(and its own package). Move the shared code into analyse/helpers/."
    )


@pytest.mark.parametrize("module", sorted((ANALYSE_ROOT / HELPERS).rglob("*.py")),
                         ids=lambda p: p.name)
def test_helpers_imports_no_analysis(module: Path) -> None:
    """``helpers`` is the bottom of this package: it may not depend on any analysis."""
    offending = _imported_analyse_packages(module) - {HELPERS}
    assert not offending, (
        f"analyse/helpers/{module.name} imports analysis package(s) {sorted(offending)}. "
        f"That inverts the dependency — helpers is imported BY the analyses, never the "
        f"other way round."
    )
