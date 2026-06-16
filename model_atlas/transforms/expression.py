"""Restricted arithmetic-expression evaluator for the transform pipe.

Defines:    evaluate() — a sandboxed evaluator that binds one variable ``value``
            and allows only arithmetic, comparison and a whitelist of builtins.
Used by:    transforms.builtin (the ``arithmetic`` transform).
Depends on: standard library (ast) only.

This is the canonical home of the expression sandbox. The legacy
transforms/timestamp.py carries its own copy and is removed when the pipeline is
migrated onto the transform registry.
"""
from __future__ import annotations

import ast
import logging
from typing import Any

log = logging.getLogger(__name__)

# WHY: presets are shared/untrusted evidence config, so an expression must never
# reach arbitrary code. Only the names and node types below are permitted; the
# globals expose no builtins beyond the whitelisted callables.
_ALLOWED_NAMES = {"value", "int", "float", "str", "round", "abs"}
_SAFE_GLOBALS: dict[str, Any] = {
    "__builtins__": {},
    "int": int,
    "float": float,
    "str": str,
    "round": round,
    "abs": abs,
}
_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Call,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.IfExp,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
)


def _validate(node: ast.AST) -> None:
    for child in ast.walk(node):
        if not isinstance(child, _ALLOWED_NODES):
            raise ValueError(f"Unsupported expression syntax: {type(child).__name__}")
        if isinstance(child, ast.Name) and child.id not in _ALLOWED_NAMES:
            raise ValueError(f"Unsupported expression name: {child.id}")
        if isinstance(child, ast.Call):
            # Only whitelisted functions may be called, and ``value`` is data, not callable.
            if not isinstance(child.func, ast.Name) or child.func.id not in _ALLOWED_NAMES - {"value"}:
                raise ValueError("Expressions may only call whitelisted functions")


def evaluate(expression: str, value: Any) -> Any:
    """Evaluate ``expression`` with ``value`` bound, under the sandbox above."""
    node = ast.parse(expression, mode="eval")
    _validate(node)
    code = compile(node, "<matlas-expression>", "eval")
    result = eval(code, _SAFE_GLOBALS, {"value": value})  # noqa: S307 - AST + globals restricted above.
    log.debug("Evaluated expression %s on %r -> %r", expression, value, result)
    return result
