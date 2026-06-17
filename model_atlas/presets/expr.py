"""Mini-parser for v3 preset reference and pipe expressions.

Defines:    Ref (a typed value source), PipeCall (one transform step), and
            parse_ref / parse_pipe which turn the call-form strings used in v3
            presets into those structures.
Used by:    presets.spec (parse + validate), transforms.assemble (resolve refs),
            transforms.builtin (run pipe steps).
Depends on: standard library only.

Reference forms — the value of a mapped field is ONE explicit call:
    column(NAME)              a source column's value (NAME may be a glob; quote if spaced)
    header("Glob *")          the matched column's header text (was v2 `from_name`)
    filename(name|stem|path)  part of the source file identity (was v2 `from_file`)
    param(linked_entity)      a run-level parameter (entity | linked_entity)
    const(VALUE)              a literal (number / bool / string)

Pipe form — the procedural escape hatch, left-to-right, '|'-separated calls:
    "cast(int) | scale(3.6)"
    "lookup(recovery, on_unknown=null)"
Arguments are positional or key=value; scalar values are coerced (int/float/bool/
null/str). A bareword (unquoted) string stays a string.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

REF_KINDS = ("column", "header", "filename", "const", "param")
FILENAME_TOKENS = ("name", "stem", "path")
PARAM_TOKENS = ("entity", "linked_entity")

_CALL_RE = re.compile(r"^\s*([a-z_]+)\s*\((.*)\)\s*$", re.DOTALL)
_BARE_CALL_RE = re.compile(r"^\s*([a-z_]+)\s*$")


@dataclass(frozen=True)
class Ref:
    """A typed value source. ``kind`` is one of REF_KINDS; ``arg`` is the column
    name/glob, file token, param name, or (for ``const``) the literal value."""

    kind: str
    arg: Any


@dataclass(frozen=True)
class PipeCall:
    """One transform step: a transform ``name`` with positional/keyword arguments."""

    name: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)  # mutable default avoided via factory


def _split_top_level(text: str, sep: str) -> list[str]:
    """Split ``text`` on ``sep``, ignoring separators inside quotes or parentheses."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in text:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


def _coerce(token: str) -> Any:
    """Coerce a scalar token: quoted -> string; null/true/false; int; float; else bareword."""
    token = token.strip()
    if len(token) >= 2 and token[0] in ("'", '"') and token[-1] == token[0]:
        return token[1:-1]
    low = token.casefold()
    if low in ("null", "none"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return token


def _unquote(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] in ("'", '"') and token[-1] == token[0]:
        return token[1:-1]
    return token


def parse_ref(text: Any) -> Ref:
    """Parse a single reference expression like ``column(ZLAT)`` or ``const(device)``."""
    if not isinstance(text, str):
        raise ValueError(f"reference must be a string call like column(...), got {text!r}")
    match = _CALL_RE.match(text)
    if match is None:
        raise ValueError(
            f"reference {text!r} must be an explicit call: "
            f"column(...), header(...), filename(...), param(...), or const(...)"
        )
    kind, raw = match.group(1), match.group(2).strip()
    if kind not in REF_KINDS:
        raise ValueError(f"unknown reference kind {kind!r}; expected one of {REF_KINDS}")
    if kind == "const":
        return Ref(kind="const", arg=_coerce(raw))
    arg = _unquote(raw)
    if kind == "filename" and arg not in FILENAME_TOKENS:
        raise ValueError(f"filename(...) takes one of {FILENAME_TOKENS}, got {arg!r}")
    if kind == "param" and arg not in PARAM_TOKENS:
        raise ValueError(f"param(...) takes one of {PARAM_TOKENS}, got {arg!r}")
    if kind in ("column", "header") and not arg:
        raise ValueError(f"{kind}(...) needs a column name or glob")
    return Ref(kind=kind, arg=arg)


def parse_pipe(text: Any) -> tuple[PipeCall, ...]:
    """Parse a pipe chain like ``"cast(int) | lookup(recovery, on_unknown=null)"``."""
    if text is None:
        return ()
    if not isinstance(text, str):
        raise ValueError(f"pipe must be a string call-chain, got {text!r}")
    steps: list[PipeCall] = []
    for raw_step in _split_top_level(text, "|"):
        raw_step = raw_step.strip()
        if not raw_step:
            continue
        bare = _BARE_CALL_RE.match(raw_step)
        if bare is not None:
            steps.append(PipeCall(name=bare.group(1)))
            continue
        match = _CALL_RE.match(raw_step)
        if match is None:
            raise ValueError(f"pipe step {raw_step!r} must be a call like name(args)")
        name, raw_args = match.group(1), match.group(2).strip()
        args: list[Any] = []
        kwargs: dict[str, Any] = {}
        if raw_args:
            for token in _split_top_level(raw_args, ","):
                token = token.strip()
                if not token:
                    continue
                key, eq, value = _split_kwarg(token)
                if eq:
                    kwargs[key] = _coerce(value)
                else:
                    args.append(_coerce(token))
        steps.append(PipeCall(name=name, args=tuple(args), kwargs=kwargs))
    return tuple(steps)


def _split_kwarg(token: str) -> tuple[str, bool, str]:
    """Return (key, is_kwarg, value) for a ``key=value`` token (top-level '=' only)."""
    halves = _split_top_level(token, "=")
    if len(halves) >= 2:
        return halves[0].strip(), True, "=".join(halves[1:]).strip()
    return token, False, token
