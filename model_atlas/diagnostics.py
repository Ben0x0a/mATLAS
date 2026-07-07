"""Forensic crash-report handler: on any unhandled exception, write one structured JSON
artefact capturing the traceback, every stack frame's variables (size-limited and redacted),
and full execution metadata — enough to debug a run without reproducing it.

Defines:    _summarise (the value size-limiter), _collect_frames (per-frame locals),
            _metadata (environment), write_crash_report, and install_excepthook.
Used by:    launcher.cli and launcher.gui (install the hook at start-up); model_atlas is
            the tool the artefact attributes itself to.
Depends on: model_atlas.config (DiagnosticsSettings) and the STANDARD LIBRARY ONLY. pandas
            and numpy are detected structurally (by type name), never imported, so this
            module stays import-light and works on Python 3.11 -> 3.14.

Design:
- A global ``sys.excepthook`` (and ``threading.excepthook``) receives ``(type, exc, tb)``.
  Walking ``tb`` yields every frame; each frame's ``f_locals`` holds its variables, so the
  offending value AND its surrounding context (e.g. the source ``record`` in ``build_rows``)
  are captured with no try/except sprinkled through the codebase.
- Each value passes through ``_summarise`` which NEVER dumps a whole DataFrame/collection:
  it records shape/columns/head, hashes long strings, caps items and recursion depth, and
  keeps a per-frame byte budget. Variable names matching the configured redaction tokens are
  replaced by a type + sha256 (correlatable, but not the plaintext secret).
- The handler is fully guarded: any failure inside it degrades to a log line and returns
  ``None`` — it never masks the original exception, which still prints its normal traceback.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import linecache
import logging
import os
import platform
import sys
import threading
import traceback
from importlib import metadata as importlib_metadata
from pathlib import Path, PurePath
from typing import Any

from model_atlas import __version__
from model_atlas.config import DiagnosticsSettings, get_settings

log = logging.getLogger(__name__)

# Bumped when the report structure changes, so a downstream reader can branch on it.
REPORT_SCHEMA = "matlas.crash-report/1"

# Packages whose installed version is worth recording for a repro (best-effort; a missing
# one is simply omitted). model_atlas' own version is recorded separately from __version__.
_RECORDED_PACKAGES = ("pandas", "numpy", "PySide6", "openpyxl", "PyYAML")


# --- redaction --------------------------------------------------------------

def _is_redacted(name: str | None, cfg: DiagnosticsSettings) -> bool:
    """True when a variable/key name matches any configured redaction token (case-insensitive
    substring). A matched value is hashed, never emitted — a leaked secret is unrecoverable."""
    if not name:
        return False
    lowered = name.lower()
    return any(token in lowered for token in cfg.redact)


# --- path anonymisation -----------------------------------------------------
# Filesystem paths leak the examiner's username and internal directory structure. A path under
# the mATLAS project (script) directory is shown RELATIVE to it — so a source file reads as
# "model_atlas/transforms/builtin.py" — which keeps the useful part and drops the machine root.
# Only paths OUTSIDE the project (other resources: site-packages, home files) fall back to
# home-stripping ("~/..."). Input-file Path *values* are redacted harder (type + size) in
# _summarise. Both roots are stable, so they are resolved once at import.

try:
    # diagnostics.py lives at <repo>/model_atlas/diagnostics.py, so parent.parent is the root.
    _PROJECT_ROOT: Path | None = Path(__file__).resolve().parent.parent
except Exception:  # noqa: BLE001 - root discovery is best-effort; None disables project-relative.
    _PROJECT_ROOT = None

try:
    _HOME: Path | None = Path.home()
except Exception:  # noqa: BLE001
    _HOME = None


def _relativise_path(path_str: str, cfg: DiagnosticsSettings, *, basename_fallback: bool) -> str:
    """Render a single path RELATIVE to the project dir; else ``~/...`` when under home; else the
    bare basename (source files, which are definitely paths) or the original string unchanged
    (arbitrary str locals, which may not be paths and must not be mangled)."""
    if not cfg.redact_paths or not path_str:
        return path_str
    try:
        path = Path(path_str)
    except Exception:  # noqa: BLE001 - a non-path value (e.g. "<string>") is returned as-is.
        return path_str
    if _PROJECT_ROOT is not None:
        try:
            return path.relative_to(_PROJECT_ROOT).as_posix()
        except (ValueError, TypeError):
            pass
    if _HOME is not None:
        try:
            return "~/" + path.relative_to(_HOME).as_posix()
        except (ValueError, TypeError):
            pass
    return path.name if basename_fallback else path_str


def _anonymise_paths(text: str, cfg: DiagnosticsSettings) -> str:
    """For arbitrary TEXT that may EMBED a path (traceback, exception message, notes): make a
    project path relative in place, then strip the home root to ``~``. Substring-based because
    the path is not the whole string here."""
    if not cfg.redact_paths or not text:
        return text
    if _PROJECT_ROOT is not None:
        root = str(_PROJECT_ROOT)
        text = text.replace(root + os.sep, "")   # project files -> relative (drop the root)
        text = text.replace(root, ".")           # the project root itself -> "."
    if _HOME is not None:
        text = text.replace(str(_HOME), "~")
    return text


def _safe_size(path: Any) -> int | None:
    """File size in bytes via a single stat (metadata only — never reads/hashes the file, so a
    512 GB acquisition costs nothing). None when the path does not exist or is not stattable."""
    stat = getattr(path, "stat", None)
    if stat is None:
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None


# --- safe primitives (must never raise: a broken __repr__ cannot crash the handler) --------

def _typename(value: Any) -> str:
    cls = type(value)
    module = getattr(cls, "__module__", "") or ""
    return cls.__qualname__ if module in ("builtins", "") else f"{module}.{cls.__qualname__}"


def _safe_repr(value: Any, limit: int) -> tuple[str | None, str | None]:
    """(repr-or-None, error-or-None). Truncates past ``limit`` with a residual-length marker."""
    try:
        text = repr(value)
    except Exception as exc:  # noqa: BLE001 - a value's __repr__ must not crash the handler.
        return None, f"{type(exc).__name__}: {exc}"
    if len(text) > limit:
        text = text[:limit] + f"…<+{len(text) - limit} chars>"
    return text, None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_of(value: Any) -> str | None:
    try:
        return _sha256(repr(value).encode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - hashing is best-effort provenance, never fatal.
        return None


# --- structural type probes (no pandas/numpy import) -----------------------

def _looks_like(value: Any, name: str, module_prefix: str) -> bool:
    """True when ``value``'s type (or any base) is called ``name`` from ``module_prefix.*`` —
    lets us recognise DataFrame/Series/ndarray without importing pandas or numpy."""
    try:
        for base in type(value).__mro__:
            if base.__name__ == name and getattr(base, "__module__", "").startswith(module_prefix):
                return True
    except Exception:  # noqa: BLE001 - exotic metaclasses must not crash the probe.
        return False
    return False


# --- the summariser ---------------------------------------------------------

def _summarise(name: str | None, value: Any, cfg: DiagnosticsSettings, depth: int) -> Any:
    """Return a JSON-able summary of ``value`` under the variable/key ``name``.

    Redaction (by name) wins first; then a type-dispatched summary that caps size at every
    turn. ``depth`` counts DOWN: at <= 0 a container degrades to a shallow repr instead of
    recursing, so a deeply nested structure can never blow up the report."""
    if _is_redacted(name, cfg):
        return {"redacted": True, "type": _typename(value), "sha256": _sha256_of(value)}
    return _summarise_value(value, cfg, depth)


def _summarise_value(value: Any, cfg: DiagnosticsSettings, depth: int) -> Any:
    # Scalars flow through verbatim — they are the whole point of "capture every variable".
    if value is None or isinstance(value, (bool, int, float)):
        return value

    # A filesystem path VALUE (e.g. the input acquisition): keep type + size only. The name and
    # directory are dropped entirely — both can leak the evidence identity / examiner structure.
    if isinstance(value, PurePath):
        if cfg.redact_paths:
            return {"type": _typename(value), "size_bytes": _safe_size(value), "redacted_path": True}
        return {"type": _typename(value), "path": str(value), "size_bytes": _safe_size(value)}

    if isinstance(value, str):
        # BuildEnv stores paths as str, so treat the whole value as a path: relative-to-project,
        # else ~/… , else left unchanged (a non-path string is returned untouched).
        value = _relativise_path(value, cfg, basename_fallback=False)
        if len(value) <= cfg.max_string_len:
            return value
        return {
            "type": "str", "len": len(value),
            "sha256": _sha256(value.encode("utf-8", "replace")),
            "preview": value[:cfg.max_string_len],
            "truncated": True,
        }

    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        return {
            "type": _typename(value), "len": len(raw), "sha256": _sha256(raw),
            "preview_hex": raw[:64].hex(),
            "truncated": len(raw) > 64,
        }

    # pandas / numpy: summarise structure + a tiny head only — never the whole container.
    if _looks_like(value, "DataFrame", "pandas"):
        return _summarise_dataframe(value, cfg, depth)
    if _looks_like(value, "Series", "pandas"):
        return _summarise_series(value, cfg, depth)
    if _looks_like(value, "ndarray", "numpy"):
        return _summarise_ndarray(value, cfg)

    if isinstance(value, dict):
        return _summarise_mapping(value, cfg, depth)

    if isinstance(value, (list, tuple, set, frozenset)):
        return _summarise_sequence(value, cfg, depth)

    return _summarise_object(value, cfg, depth)


def _shallow(value: Any, cfg: DiagnosticsSettings) -> Any:
    """A depth-exhausted fallback: type + truncated repr, no recursion."""
    text, err = _safe_repr(value, cfg.max_string_len)
    out: dict[str, Any] = {"type": _typename(value), "depth_exceeded": True}
    if err is not None:
        out["repr_error"] = err
    else:
        out["repr"] = text
    return out


def _summarise_mapping(value: dict, cfg: DiagnosticsSettings, depth: int) -> Any:
    if depth <= 0:
        return _shallow(value, cfg)
    items: dict[str, Any] = {}
    for i, (key, val) in enumerate(value.items()):
        if i >= cfg.max_items:
            break
        # A non-string key is rendered via repr so the JSON object stays valid.
        key_name = key if isinstance(key, str) else repr(key)
        items[str(key_name)] = _summarise(str(key_name), val, cfg, depth - 1)
    out: dict[str, Any] = {"type": _typename(value), "len": len(value), "items": items}
    if len(value) > cfg.max_items:
        out["truncated"] = True
    return out


def _summarise_sequence(value: Any, cfg: DiagnosticsSettings, depth: int) -> Any:
    if depth <= 0:
        return _shallow(value, cfg)
    materialised = list(value)
    items = [_summarise(None, item, cfg, depth - 1) for item in materialised[:cfg.max_items]]
    out: dict[str, Any] = {"type": _typename(value), "len": len(materialised), "items": items}
    if len(materialised) > cfg.max_items:
        out["truncated"] = True
    return out


def _summarise_object(value: Any, cfg: DiagnosticsSettings, depth: int) -> Any:
    text, err = _safe_repr(value, cfg.max_string_len)
    out: dict[str, Any] = {"type": _typename(value)}
    if err is not None:
        out["repr_error"] = err
    else:
        out["repr"] = text
    # A dataclass/plain object: capture its instance attributes shallowly for extra context.
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict) and attrs and depth > 0:
        shown: dict[str, Any] = {}
        for i, (key, val) in enumerate(attrs.items()):
            if i >= cfg.max_items:
                out["attrs_truncated"] = True
                break
            shown[str(key)] = _summarise(str(key), val, cfg, depth - 1)
        out["attrs"] = shown
    return out


def _summarise_dataframe(value: Any, cfg: DiagnosticsSettings, depth: int) -> Any:
    """Structure + head only. Wrapped defensively: a broken frame yields a repr fallback."""
    out: dict[str, Any] = {"type": _typename(value)}
    try:
        out["shape"] = list(value.shape)
        out["columns"] = [str(c) for c in list(value.columns)[:cfg.max_items]]
        out["dtypes"] = {str(k): str(v) for k, v in list(value.dtypes.items())[:cfg.max_items]}
        try:
            out["memory_bytes"] = int(value.memory_usage(deep=True).sum())
        except Exception:  # noqa: BLE001 - memory_usage can fail on exotic dtypes.
            pass
        head_records = value.head(cfg.max_head).to_dict(orient="records")
        out["head"] = [
            {str(k): _summarise(str(k), v, cfg, depth - 1) for k, v in record.items()}
            for record in head_records
        ]
    except Exception as exc:  # noqa: BLE001 - never let frame introspection crash the handler.
        out["summary_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _summarise_series(value: Any, cfg: DiagnosticsSettings, depth: int) -> Any:
    out: dict[str, Any] = {"type": _typename(value)}
    try:
        out["len"] = int(value.shape[0])
        out["dtype"] = str(value.dtype)
        head = value.head(cfg.max_head).tolist()
        out["head"] = [_summarise(None, v, cfg, depth - 1) for v in head]
    except Exception as exc:  # noqa: BLE001
        out["summary_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _summarise_ndarray(value: Any, cfg: DiagnosticsSettings) -> Any:
    out: dict[str, Any] = {"type": _typename(value)}
    try:
        out["shape"] = list(value.shape)
        out["dtype"] = str(value.dtype)
        out["size"] = int(value.size)
        preview = value.flatten()[:cfg.max_items].tolist()
        out["preview"] = preview
        if int(value.size) > cfg.max_items:
            out["truncated"] = True
    except Exception as exc:  # noqa: BLE001
        out["summary_error"] = f"{type(exc).__name__}: {exc}"
    return out


# --- frame + metadata collection -------------------------------------------

def _sizeof(summary: Any) -> int:
    try:
        return len(json.dumps(summary, default=str, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - a non-serialisable summary still has a str length.
        return len(str(summary))


def _collect_frames(tb: Any, cfg: DiagnosticsSettings) -> list[dict[str, Any]]:
    """One entry per stack frame (outermost first, crash site last), each with its source line
    and summarised locals. A per-frame byte budget stops one huge frame bloating the report."""
    frames: list[dict[str, Any]] = []
    for frame, lineno in traceback.walk_tb(tb):
        code = frame.f_code
        locals_summary: dict[str, Any] = {}
        used = 0
        truncated = False
        for var_name, value in list(frame.f_locals.items()):
            summary = _summarise(var_name, value, cfg, cfg.max_depth)
            size = _sizeof(summary)
            if used + size > cfg.max_frame_bytes:
                truncated = True
                break
            locals_summary[var_name] = summary
            used += size
        entry: dict[str, Any] = {
            # co_filename is used verbatim for linecache (must be the real path) but anonymised
            # in the report so the username / directory structure does not leak.
            "file": _relativise_path(code.co_filename, cfg, basename_fallback=True),
            "lineno": lineno,
            "function": code.co_name,
            "code": _anonymise_paths(
                linecache.getline(code.co_filename, lineno).strip(), cfg) or None,
            "locals": locals_summary,
        }
        if truncated:
            entry["locals_truncated"] = True
        frames.append(entry)
    return frames


def _package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in _RECORDED_PACKAGES:
        try:
            versions[name] = importlib_metadata.version(name)
        except Exception:  # noqa: BLE001 - an absent optional package is simply omitted below.
            continue
    return versions


def _git_commit() -> str | None:
    """Best-effort short commit: read .git/HEAD from the CWD upward, no subprocess."""
    try:
        for directory in (Path.cwd(), *Path.cwd().parents):
            head = directory / ".git" / "HEAD"
            if not head.is_file():
                continue
            content = head.read_text(encoding="utf-8").strip()
            if content.startswith("ref:"):
                ref = (directory / ".git" / content[4:].strip())
                return ref.read_text(encoding="utf-8").strip() if ref.is_file() else None
            return content  # detached HEAD stores the commit sha directly
    except Exception:  # noqa: BLE001 - provenance is best-effort, never fatal.
        return None
    return None


def _metadata(cfg: DiagnosticsSettings, context: dict[str, Any] | None) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    md: dict[str, Any] = {
        "tool": "mATLAS",
        "tool_version": __version__,
        "generated_at_utc": now.isoformat(),
        "generated_at_local": now.astimezone().isoformat(),
        "python_version": sys.version,
        "python_implementation": platform.python_implementation(),
        "executable": _relativise_path(sys.executable, cfg, basename_fallback=False),
        "argv": [_relativise_path(arg, cfg, basename_fallback=False) for arg in sys.argv],
        "pid": os.getpid(),
        "cwd": _relativise_path(str(Path.cwd()), cfg, basename_fallback=False),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "node": platform.node(),
        },
        "packages": _package_versions(),
        "git_commit": _git_commit(),
    }
    try:
        md["settings"] = {
            key: (_anonymise_paths(val, cfg) if isinstance(val, str) else val)
            for key, val in get_settings().to_traceability().items()
        }
    except Exception as exc:  # noqa: BLE001 - a broken config is itself a likely crash cause.
        md["settings_error"] = f"{type(exc).__name__}: {exc}"
    if cfg.capture_env:
        # Every value passes through redaction by its NAME, so a matched secret is hashed.
        md["environ"] = {k: _summarise(k, v, cfg, 1) for k, v in os.environ.items()}
    if context:
        md["context"] = context
    return md


# --- report assembly + hook -------------------------------------------------

def _diag_settings() -> DiagnosticsSettings:
    """Resolved diagnostics config, falling back to defaults if the config itself is broken
    (so a bad-config crash still produces a report rather than being swallowed here)."""
    try:
        return get_settings().diagnostics
    except Exception:  # noqa: BLE001 - defaults keep the handler working when config load fails.
        return DiagnosticsSettings()


def _report_path(cfg: DiagnosticsSettings) -> Path:
    """A Windows-safe, collision-free path: no colons in the timestamp; pid suffix; and a
    numeric suffix only if a same-second same-pid file already exists (never overwrite)."""
    directory = Path(cfg.crash_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    base = f"matlas_error_{stamp}_p{os.getpid()}"
    path = directory / f"{base}.json"
    counter = 1
    while path.exists():
        path = directory / f"{base}_{counter}.json"
        counter += 1
    return path


def write_crash_report(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: Any,
    *,
    context: dict[str, Any] | None = None,
) -> Path | None:
    """Write one JSON crash report and return its path (or None if disabled/failed).

    Guaranteed non-throwing: any internal failure is logged and swallowed so the original
    exception is never masked."""
    try:
        cfg = _diag_settings()
        if not cfg.enabled:
            return None
        report = {
            "schema": REPORT_SCHEMA,
            "tool": "mATLAS",
            "exception": {
                "type": _typename(exc),
                "message": _anonymise_paths(str(exc), cfg),
                "args": [_summarise(None, arg, cfg, 1) for arg in getattr(exc, "args", ())],
                # __notes__ carries the build_rows breadcrumb (source/table/line/field/value).
                "notes": [_anonymise_paths(str(n), cfg)
                          for n in (getattr(exc, "__notes__", []) or [])] or None,
            },
            "traceback": _anonymise_paths("".join(traceback.format_exception(exc_type, exc, tb)), cfg),
            "frames": _collect_frames(tb, cfg),
            "metadata": _metadata(cfg, context),
        }
        path = _report_path(cfg)
        # utf-8-sig so Windows tooling detects the encoding (house encoding rule).
        path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8-sig",
        )
        log.error(f"Crash report written to {path}")
        return path
    except Exception:  # noqa: BLE001 - the handler must never raise over the original error.
        log.exception("Failed to write crash report")
        return None


_installed = False


def install_excepthook(context: dict[str, Any] | None = None) -> None:
    """Install the crash handler on both ``sys.excepthook`` and ``threading.excepthook``,
    chaining the previous hooks so the normal stderr traceback still prints. Idempotent."""
    global _installed
    if _installed:
        return

    previous_sys_hook = sys.excepthook

    def _sys_hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        write_crash_report(exc_type, exc, tb, context=context)
        previous_sys_hook(exc_type, exc, tb)

    sys.excepthook = _sys_hook

    previous_thread_hook = threading.excepthook

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        thread_ctx = dict(context or {})
        if args.thread is not None:
            thread_ctx["thread"] = args.thread.name
        if args.exc_value is not None:
            write_crash_report(args.exc_type, args.exc_value, args.exc_traceback, context=thread_ctx)
        previous_thread_hook(args)

    threading.excepthook = _thread_hook
    _installed = True
