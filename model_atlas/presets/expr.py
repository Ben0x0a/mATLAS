"""Mini-parser for v3 preset reference, pipe and conditional expressions.

Defines:    Ref (a typed value source), PipeCall (one transform step), Cond / CondBranch
            (a conditional mapping value) plus the ``NA_SENTINEL`` it yields, and
            parse_ref / parse_pipe / parse_cond which turn the call-form strings used in
            v3 presets into those structures.
Used by:    presets.spec (parse + validate), transforms.assemble (resolve refs + conditions),
            transforms.builtin (run pipe steps).
Depends on: standard library only.

Reference forms — the value of a mapped field is ONE explicit call:
    field(NAME)               a source field's value (NAME may be a glob; quote if spaced)
    header("Glob *")          the matched field's header text (was v2 `from_name`)
    filename(name|stem|path)  part of the source file identity (was v2 `from_file`)
    param(concerns)      a run-level parameter (entity | concerns)
    preset(in_archive)        a key from the CURRENT preset's own definition (match/meta)
    const(VALUE)              a literal (number / bool / string)
    format("{field(A)} at {field(B)}")   an f-string-style template: literal
                              text with ``{<ref>}`` placeholders (each an existing ref);
                              ``{{`` / ``}}`` are literal braces

Conditional form — a mapping value may instead be an if/elif/else chain
over those same refs; branches evaluate top-to-bottom, first match wins::

    if field(X) == "NA" then const(instant)
    if field(X) == "NA" then const(instant) else const(continuous)
    if field(X) == "NA" then const(instant) elif field(Y) == "1" then const(interval)
        else const(continuous)

The left operand of each test is a ref; the comparand is a ref OR a bare literal (a
bare/quoted ``NA`` is the explicit "value is missing" test). With no ``else`` and no
matching branch the chain yields ``NA_SENTINEL`` — DELIBERATELY distinct from an
unmapped field (which is None): ``NA_SENTINEL`` means "the conditional was evaluated and
nothing matched", None means "never mapped".

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

REF_KINDS = ("field", "header", "filename", "const", "param", "preset", "format")
FILENAME_TOKENS = ("name", "stem", "path")
PARAM_TOKENS = ("entity", "concerns")
# Keys a preset(...) ref may read from the current preset (match block + meta).
PRESET_TOKENS = (
    "path", "name", "format", "table", "sheet", "sql",
    "id", "tier", "os", "tool", "version", "os_version",
)

_CALL_RE = re.compile(r"^\s*([a-z_]+)\s*\((.*)\)\s*$", re.DOTALL)
_BARE_CALL_RE = re.compile(r"^\s*([a-z_]+)\s*$")

# Conditional keywords + comparison operators.
_COND_OPS = ("==", "!=")
_COND_PREFIX_RE = re.compile(r"^\s*if\s+", re.IGNORECASE)
# The comparand spelling that means "the tested value is missing/absent" rather than a
# literal string comparison. Both `"NA"` (quoted) and bare `NA` coerce to this string.
NA_LITERAL = "NA"


class _NASentinel(str):
    """The value a conditional or template yields when it was
    evaluated but produced nothing — a conditional with no matching branch and no ``else``,
    or a template whose every placeholder was missing.

    It is a ``str`` subclass whose text is exactly ``N/A`` (the same spelling as the existing
    ``LOCAL_OFFSET_UNKNOWN`` sentinel), chosen so it:
    - STORES/SERIALISES as the plain TEXT ``N/A`` — SQLite-bindable, renders in the CSV, and
      ALWAYS distinguishable from None (the "field was never mapped" case, which is NULL);
    - keeps a distinct TYPE, so in-process resolution recognises it by IDENTITY
      (``value is NA_SENTINEL``) and a real source cell that happens to read ``N/A`` (an
      ordinary ``str``) is never mistaken for it and never short-circuits the transform tail.

    (After a SQLite round-trip the value returns as a plain ``str`` ``N/A`` — the type, not
    the value, is what is lost, so downstream still sees a distinct-from-None sentinel.)"""

    _instance: "_NASentinel | None" = None

    def __new__(cls) -> "_NASentinel":
        # HOW: cache the sole instance so every "produced nothing" resolves to the SAME object.
        # WHY: identity is the in-process contract — a fresh object per call would defeat the
        # ``is NA_SENTINEL`` test used to short-circuit the transform tail.
        if cls._instance is None:
            cls._instance = super().__new__(cls, "N/A")
        return cls._instance


# The single sentinel instance shared across the whole engine (produced by conditional /
# template resolution in transforms.assemble; the successor engine imports it unchanged).
NA_SENTINEL = _NASentinel()


@dataclass(frozen=True)
class Ref:
    """A typed value source. ``kind`` is one of REF_KINDS; ``arg`` is the field
    name/glob, file token, param name, or (for ``const``) the literal value."""

    kind: str
    arg: Any


@dataclass(frozen=True)
class Template:
    """A ``format(...)`` template: an ordered sequence of segments, each either
    a literal ``str`` or an embedded ``Ref`` placeholder. Carried as the ``arg`` of a
    ``Ref(kind="format")`` so it composes with conditionals and the pipe tail for free."""

    segments: tuple[Any, ...]   # str literals interleaved with Ref placeholders

    def refs(self) -> tuple["Ref", ...]:
        """The placeholder refs, in order — for the linter / field-inventory helpers."""
        return tuple(seg for seg in self.segments if isinstance(seg, Ref))


@dataclass(frozen=True)
class PipeCall:
    """One transform step: a transform ``name`` with positional/keyword arguments."""

    name: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)  # mutable default avoided via factory


@dataclass(frozen=True)
class CondBranch:
    """One ``if``/``elif`` branch: ``then`` is resolved when ``test <op> comparand`` holds.
    ``test`` is a value ref (usually ``field(...)``); ``comparand`` is a ref or a literal
    wrapped as ``const(...)`` — a ``const(NA)`` comparand is the explicit missing-value test."""

    test: Ref
    op: str            # one of _COND_OPS
    comparand: Ref
    then: Ref


@dataclass(frozen=True)
class Cond:
    """A conditional mapping value: ordered branches (first match wins) + an optional
    ``else_``. With no match and no ``else_`` the resolver yields ``NA_SENTINEL``."""

    branches: tuple[CondBranch, ...]
    else_: Ref | None = None

    def refs(self) -> tuple[Ref, ...]:
        """Every Ref this conditional mentions — tests, comparands, results — so the linter
        and field-inventory helpers can see the source fields a conditional reads."""
        out: list[Ref] = []
        for branch in self.branches:
            out.extend((branch.test, branch.comparand, branch.then))
        if self.else_ is not None:
            out.append(self.else_)
        return tuple(out)


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
    """Parse a single reference expression like ``field(ZLAT)`` or ``const(device)``."""
    if not isinstance(text, str):
        raise ValueError(f"reference must be a string call like field(...), got {text!r}")
    match = _CALL_RE.match(text)
    if match is None:
        raise ValueError(
            f"reference {text!r} must be an explicit call: "
            f"field(...), header(...), filename(...), param(...), or const(...)"
        )
    kind, raw = match.group(1), match.group(2).strip()
    if kind not in REF_KINDS:
        raise ValueError(f"unknown reference kind {kind!r}; expected one of {REF_KINDS}")
    if kind == "const":
        return Ref(kind="const", arg=_coerce(raw))
    if kind == "format":
        return Ref(kind="format", arg=parse_template(_unquote(raw)))
    arg = _unquote(raw)
    if kind == "filename" and arg not in FILENAME_TOKENS:
        raise ValueError(f"filename(...) takes one of {FILENAME_TOKENS}, got {arg!r}")
    if kind == "param" and arg not in PARAM_TOKENS:
        raise ValueError(f"param(...) takes one of {PARAM_TOKENS}, got {arg!r}")
    if kind == "preset" and arg not in PRESET_TOKENS:
        raise ValueError(f"preset(...) takes one of {PRESET_TOKENS}, got {arg!r}")
    if kind in ("field", "header") and not arg:
        raise ValueError(f"{kind}(...) needs a field name or glob")
    return Ref(kind=kind, arg=arg)


def parse_template(text: str) -> Template:
    """Parse a ``format(...)`` template body into a ``Template``.

    HOW: scan the (already unquoted) text left to right. ``{{`` / ``}}`` emit a literal brace
    (f-string convention); a ``{`` opens a placeholder whose matching ``}`` is found while
    honouring nested parentheses/quotes (so ``{field("a}b")}`` is not split at the inner
    brace), and the enclosed text is parsed as a call-form ref via ``parse_ref``; everything
    else is literal text. WHY brace-matching rather than a regex: a placeholder holds a full
    ref call that may itself contain braces or parens inside a quoted argument.

    Raises ``ValueError`` on an unbalanced brace, an empty ``{}`` placeholder, an unknown ref
    kind inside braces, or an empty template — a preset authoring error surfaced at load."""
    segments: list[Any] = []
    literal: list[str] = []
    index = 0
    total = len(text)

    def _flush_literal() -> None:
        if literal:
            segments.append("".join(literal))
            literal.clear()

    while index < total:
        char = text[index]
        if char == "{":
            if index + 1 < total and text[index + 1] == "{":   # escaped literal brace
                literal.append("{")
                index += 2
                continue
            end = _find_close_brace(text, index + 1)
            if end == -1:
                raise ValueError(f"format template {text!r}: unbalanced '{{'")
            inner = text[index + 1:end].strip()
            if not inner:
                raise ValueError(f"format template {text!r}: empty '{{}}' placeholder")
            _flush_literal()
            segments.append(parse_ref(inner))   # raises on an unknown ref kind
            index = end + 1
            continue
        if char == "}":
            if index + 1 < total and text[index + 1] == "}":    # escaped literal brace
                literal.append("}")
                index += 2
                continue
            raise ValueError(f"format template {text!r}: unbalanced '}}'")
        literal.append(char)
        index += 1
    _flush_literal()
    if not segments:
        raise ValueError(f"format template {text!r}: empty template")
    return Template(segments=tuple(segments))


def _find_close_brace(text: str, start: int) -> int:
    """Index of the ``}`` that closes a placeholder opened before ``start``, at parenthesis
    depth 0 and outside quotes; -1 if there is none."""
    depth = 0
    quote: str | None = None
    for index in range(start, len(text)):
        char = text[index]
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "}" and depth == 0:
            return index
    return -1


def flatten_ref(ref: Ref) -> tuple[Ref, ...]:
    """Expand a composite ref into its atomic refs: a ``format`` template yields its
    placeholder refs (recursively); any other ref yields itself. Lets the linter and
    field-inventory helpers see the source fields a template reads."""
    if ref.kind == "format":
        out: list[Ref] = []
        for embedded in ref.arg.refs():
            out.extend(flatten_ref(embedded))
        return tuple(out)
    return (ref,)


def _parse_operand(token: str) -> Ref:
    """Parse a conditional comparand: a call-form ref (``field(Y)``) stays a ref; anything
    else (``"NA"``, ``NA``, ``"1"``, ``42``) becomes ``const(<coerced-literal>)`` so both the
    test's left operand and its comparand resolve through the one ``_resolve_ref`` path.

    A token that LOOKS like a call form but names an UNKNOWN kind (a typo — ``feild(Y)``) is a
    preset error, so it RAISES at parse time instead of silently degrading to the unmatchable
    string literal ``"feild(Y)"`` — which would make every comparison quietly take the
    else-branch with no error. Quote a genuine literal (``"foo(bar)"``) to keep it a string."""
    match = _CALL_RE.match(token)
    if match is not None:
        if match.group(1) in REF_KINDS:
            return parse_ref(token)
        raise ValueError(
            f"conditional comparand {token!r} looks like a call but {match.group(1)!r} is not a "
            f"reference kind; expected one of {REF_KINDS} (quote it to use a literal string)")
    return Ref(kind="const", arg=_coerce(token))


def is_condition(text: Any) -> bool:
    """True when a mapping value is a conditional chain (starts with the ``if`` keyword)."""
    return isinstance(text, str) and _COND_PREFIX_RE.match(text) is not None


def parse_cond(text: str) -> Cond:
    """Parse ``if <ref> <op> <operand> then <ref> (elif <ref> <op> <operand> then <ref>)*
    (else <ref>)?`` into a ``Cond``.

    HOW: the string is tokenised on top-level whitespace (quotes and parentheses are kept
    intact, so ``const("a b")`` / ``field(Foo Bar)`` stay single tokens), then a small state
    machine walks ``<test> <op> <comparand> then <result>`` groups separated by ``elif`` and
    closed by an optional ``else``. WHY tokenise rather than regex-split on the keywords: a
    field name or literal could itself contain ``then``/``else``; splitting on whitespace-
    delimited top-level tokens avoids matching those inside a ref or a quoted literal.

    A malformed chain raises ``ValueError`` — a preset authoring error, surfaced at load
    time (never silently), consistent with the rest of this parser."""
    if not is_condition(text):
        raise ValueError(f"conditional must start with 'if', got {text!r}")
    body = _COND_PREFIX_RE.sub("", text, count=1).strip()
    tokens = [tok for tok in _split_top_level(body, " ") if tok.strip()]

    branches: list[CondBranch] = []
    else_: Ref | None = None
    index = 0
    total = len(tokens)

    def _take(what: str) -> str:
        nonlocal index
        if index >= total:
            raise ValueError(f"conditional {text!r} is truncated: expected {what}")
        token = tokens[index]
        index += 1
        return token

    while True:
        test = parse_ref(_take("a test reference"))
        op = _take("a comparison operator")
        if op not in _COND_OPS:
            raise ValueError(f"conditional {text!r}: unknown operator {op!r}; expected one of {_COND_OPS}")
        comparand = _parse_operand(_take("a comparand"))
        keyword = _take("'then'")
        if keyword != "then":
            raise ValueError(f"conditional {text!r}: expected 'then' after the test, got {keyword!r}")
        branches.append(CondBranch(test=test, op=op, comparand=comparand, then=parse_ref(_take("a result reference"))))
        if index < total and tokens[index] == "elif":
            index += 1
            continue
        if index < total and tokens[index] == "else":
            index += 1
            else_ = parse_ref(_take("an else reference"))
        break

    if index != total:
        raise ValueError(f"conditional {text!r}: unexpected trailing tokens {tokens[index:]}")
    return Cond(branches=tuple(branches), else_=else_)


# Reference kinds a run-level param (entity / concerns) may itself be. ``param`` is
# excluded so a value cannot reference itself circularly.
PARAM_EXPR_KINDS = ("field", "header", "filename", "const", "preset")


def parse_param_expr(text: Any) -> Ref | None:
    """Interpret a run-level param value (entity / concerns).

    When ``text`` is a call-form reference (``field(NAME)``, ``header(...)``,
    ``filename(...)``, ``const(...)``, or ``preset(...)``) return the parsed Ref so the
    value is resolved per source row; otherwise (a plain literal string, or None) return
    None so the value is used verbatim for every row."""
    if not isinstance(text, str):
        return None
    match = _CALL_RE.match(text)
    if match is None or match.group(1) not in PARAM_EXPR_KINDS:
        return None
    return parse_ref(text)


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
