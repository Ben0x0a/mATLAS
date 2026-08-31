"""Forensic crash-report handler: on any unhandled exception, write a structured report
capturing the traceback, every stack frame's variables (size-limited and redacted), and
full execution metadata — enough to debug a run without reproducing it. Two artefacts are
written side by side: a ``.json`` (machine) and a ``.md`` (human).

Defines:    _summarise (the value size-limiter), _collect_frames (per-frame locals),
            _metadata (safe environment), _render_markdown (the human report),
            write_crash_report, and install_excepthook.
Used by:    launcher.cli and launcher.gui (install the hook at start-up); model_atlas is
            the tool the artefact attributes itself to.
Depends on: model_atlas.app.config (DiagnosticsSettings) and the STANDARD LIBRARY ONLY.
            pandas and numpy are detected structurally (by type name), never imported, so
            this module stays import-light and works on Python 3.11 -> 3.14.

Design:
- A global ``sys.excepthook`` (and ``threading.excepthook``) receives ``(type, exc, tb)``.
  Walking ``tb`` yields every frame; each frame's ``f_locals`` holds its variables, so the
  offending value AND its surrounding context (e.g. the source ``record`` in ``build_observations``)
  are captured with no try/except sprinkled through the codebase.
- Each value passes through ``_summarise`` which NEVER dumps a whole DataFrame/collection:
  it records shape/columns/head, hashes long strings, caps items and recursion depth, and
  keeps a per-frame byte budget. Variable names matching the configured redaction tokens are
  replaced by a type + sha256 (correlatable, but not the plaintext secret).
- The report is split into two sections so an operator can share the first without review:
  ``safe`` (structural/environmental data that cannot carry run-time case data — exception
  TYPE, frame file/line/function/code, Python/OS/package/git/settings metadata) and
  ``sensitive`` (anything derived from run-time values or operator input — the exception
  message/args/notes, the traceback text, per-frame locals, argv, environ). Name-matched
  secrets stay hashed in BOTH sections, so the split is organisational only: it never puts
  a plaintext secret on disk that redaction would otherwise have hidden.
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
from model_atlas.app.config import DiagnosticsSettings, get_settings

log = logging.getLogger(__name__)

# Bumped when the report structure changes, so a downstream reader can branch on it. /2
# introduced the safe/sensitive split and the side-by-side .md artefact.
# /3 adds ``kind`` and, for a bug report, per-thread frame groups. The name dropped
# "crash" because the same artefact now also carries an operator-triggered bug report:
# a bug that raises nothing is exactly the case where the variables are hardest to get.
REPORT_SCHEMA = "matlas.diagnostic-report/3"

# The two things a report can be. Both share ONE format, so a reader (and any future
# redaction tooling) handles them identically; only ``kind`` and the presence of an
# exception distinguish them.
KIND_ERROR = "error"
KIND_BUG = "bug"

# How many frames of one live thread's stack a bug report keeps (outermost trimmed first).
# Deliberately a module constant rather than a config knob: it bounds the reporter, not the
# tool's behaviour, and nothing an operator would tune. Deep recursion is the only case that
# reaches it, and the innermost frames — the ones holding the interesting values — are kept.
_MAX_STACK_FRAMES = 60

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
# "model_atlas/ingest/assemble.py" — which keeps the useful part and drops the machine root.
# Only paths OUTSIDE the project (other resources: site-packages, home files) fall back to
# home-stripping ("~/..."). Input-file Path *values* are redacted harder (type + size) in
# _summarise. Both roots are stable, so they are resolved once at import.

try:
    # diagnostics.py lives at <repo>/model_atlas/app/diagnostics.py, so parents[2] climbs
    # app -> model_atlas -> the repo root. Anchoring at the repo root (not the package) is
    # what makes a project file read as "model_atlas/ingest/assemble.py", "launcher/cli.py"
    # etc. — the useful, readable relative path.
    _PROJECT_ROOT: Path | None = Path(__file__).resolve().parents[2]
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


def _frame_entry(frame: Any, lineno: int, cfg: DiagnosticsSettings) -> dict[str, Any]:
    """One stack frame: where it is, its source line, and its summarised locals.

    Shared by the traceback walk (an error) and the live-thread walk (a bug report), so both
    kinds of report describe a frame identically — the whole point of one format."""
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
    return entry


def _collect_frames(tb: Any, cfg: DiagnosticsSettings) -> list[dict[str, Any]]:
    """One entry per traceback frame (outermost first, crash site last)."""
    return [_frame_entry(frame, lineno, cfg) for frame, lineno in traceback.walk_tb(tb)]


def _collect_thread_stacks(cfg: DiagnosticsSettings,
                           skip_frame: Any = None) -> list[dict[str, Any]]:
    """Every live thread's current stack, outermost frame first.

    WHY this exists: a bug that raises nothing leaves no traceback to walk, and by the time
    an operator notices the wrong behaviour the values that explain it are only in the
    running frames. ``sys._current_frames()`` is the one way to reach them from outside the
    thread. Frames belonging to the reporter itself are dropped so the report describes the
    application, not the act of reporting.

    The mapping is a snapshot of running threads: it can name a thread that finishes while
    we walk it, so every probe is guarded — a report that skips one racing thread is worth
    far more than a reporter that raises."""
    stacks: list[dict[str, Any]] = []
    by_ident = {thread.ident: thread for thread in threading.enumerate()}
    for ident, frame in sorted(sys._current_frames().items()):
        try:
            thread = by_ident.get(ident)
            chain: list[Any] = []
            current: Any = frame
            while current is not None and len(chain) < _MAX_STACK_FRAMES:
                if current is skip_frame:
                    # Everything collected so far is INNER than the reporter, so it is the
                    # reporter's own machinery. Drop it and keep walking outward into the
                    # application. (Skipping the whole thread here instead would discard the
                    # calling thread entirely — which is usually the interesting one.)
                    chain.clear()
                    current = current.f_back
                    continue
                chain.append(current)
                current = current.f_back
            if not chain:
                continue
            chain.reverse()   # outermost first, matching a traceback's order
            stacks.append({
                "thread_name": getattr(thread, "name", None) or f"thread-{ident}",
                "thread_ident": ident,
                "daemon": bool(getattr(thread, "daemon", False)),
                "is_main": thread is threading.main_thread(),
                "frames": [_frame_entry(f, f.f_lineno, cfg) for f in chain],
            })
        except Exception:  # noqa: BLE001 - one unreadable thread must not lose the others.
            log.debug("skipped a thread while collecting stacks", exc_info=True)
    return stacks


def _safe_frame(frame: dict[str, Any]) -> dict[str, Any]:
    """The shareable half of one collected frame: WHERE the code was (file/line/function) and
    the source line itself (our own code) — never the locals, which hold run-time values."""
    return {key: frame[key] for key in ("file", "lineno", "function", "code")}


def _sensitive_frame(frame: dict[str, Any]) -> dict[str, Any]:
    """The review-before-sharing half of one collected frame: its locals, keyed back to the
    frame by function + line so a reader can line it up with the safe stack."""
    entry: dict[str, Any] = {
        "function": frame["function"], "lineno": frame["lineno"], "locals": frame["locals"],
    }
    if frame.get("locals_truncated"):
        entry["locals_truncated"] = True
    return entry


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


def _metadata(cfg: DiagnosticsSettings) -> dict[str, Any]:
    """The SAFE execution metadata: environment facts that cannot contain run-time case data
    (versions, OS, git, anonymised settings/paths). argv and os.environ are NOT here — they
    can carry operator input (a case subject on the command line, secrets in the environment)
    and so belong in the sensitive section."""
    now = dt.datetime.now(dt.timezone.utc)
    md: dict[str, Any] = {
        "tool": "mATLAS",
        "tool_version": __version__,
        "generated_at_utc": now.isoformat(),
        "generated_at_local": now.astimezone().isoformat(),
        "python_version": sys.version,
        "python_implementation": platform.python_implementation(),
        "executable": _relativise_path(sys.executable, cfg, basename_fallback=False),
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
    return md


# --- config + report path ---------------------------------------------------

def _diag_settings() -> DiagnosticsSettings:
    """Resolved diagnostics config, falling back to defaults if the config itself is broken
    (so a bad-config crash still produces a report rather than being swallowed here)."""
    try:
        return get_settings().diagnostics
    except Exception:  # noqa: BLE001 - defaults keep the handler working when config load fails.
        return DiagnosticsSettings()


def report_dir(cfg: DiagnosticsSettings | None = None) -> Path:
    """The absolute folder both report kinds are written to.

    Exposed so a shell can name it (and count what is in it) without re-deriving the
    relative-path rule — ``crash_dir`` may be relative, and resolving it in two places is
    how the two would drift."""
    cfg = cfg or _diag_settings()
    directory = Path(cfg.crash_dir).expanduser()
    return directory if directory.is_absolute() else Path.cwd() / directory


def error_report_count(cfg: DiagnosticsSettings | None = None) -> int:
    """How many ERROR reports exist (bug reports deliberately excluded).

    A shell uses this to decide whether anything has actually failed. Counting bug reports
    here would be self-fulfilling: filing one would make the tool claim it had errored."""
    try:
        return len(list(report_dir(cfg).glob(f"{_FILE_STEM[KIND_ERROR]}*.json")))
    except Exception:  # noqa: BLE001 - an unreadable folder is "nothing to show", not a crash.
        return 0


# The filename stem per kind: distinct so error and bug reports stay countable apart on
# disk, which is what lets the GUI tell "something failed" from "someone filed a report".
_FILE_STEM = {KIND_ERROR: "matlas_error_", KIND_BUG: "matlas_bug_"}


def _report_path(cfg: DiagnosticsSettings, kind: str) -> Path:
    """A Windows-safe, collision-free path: no colons in the timestamp; pid suffix; and a
    numeric suffix only if a same-second same-pid file already exists (never overwrite)."""
    directory = report_dir(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    base = f"{_FILE_STEM[kind]}{stamp}_p{os.getpid()}"
    path = directory / f"{base}.json"
    counter = 1
    while path.exists():
        path = directory / f"{base}_{counter}.json"
        counter += 1
    return path


# --- human Markdown rendering -----------------------------------------------

def _md_value(value: Any) -> str:
    """One local's value rendered inline for the Markdown report. A redacted value shows its
    type + a short sha256 (never the plaintext); anything else is compact JSON, truncated."""
    if isinstance(value, dict) and value.get("redacted") is True:
        digest = value.get("sha256") or "?"
        return f"_redacted_ (`{value.get('type', '?')}`, sha256 `{digest[:12]}…`)"
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - a non-serialisable summary still has a str form.
        text = str(value)
    if len(text) > 200:
        text = text[:200] + "…"
    return f"`{text}`"


def _render_markdown(report: dict[str, Any]) -> str:
    """Render the report as a human Markdown document with the safe part first and the
    sensitive part clearly fenced, so an operator can read it and cut the sensitive section
    before sharing. Pure/defensive: it only reads ``report`` and is called inside the
    handler's guard, so a rendering slip never masks the original crash."""
    safe = report.get("safe", {})
    sensitive = report.get("sensitive", {})
    md = safe.get("metadata", {})
    is_bug = report.get("kind") == KIND_BUG
    lines: list[str] = [
        "# mATLAS bug report" if is_bug else "# mATLAS crash report",
        "",
        f"_Schema {report.get('schema', '?')} — generated {md.get('generated_at_utc', '?')}_",
        "",
        # A bug report is filed BECAUSE the tool did something wrong without failing, so the
        # reader needs to be told what to look for; a crash report leads with its exception.
        ("_Filed by the operator: the tool did not raise, so what follows is the live state "
         "of every thread at the moment the report was requested. Describe the wrong "
         "behaviour under Notes below._" if is_bug else ""),
        "",
        "## Safe (shareable)",
        "",
        ("" if is_bug else f"**Exception:** `{safe.get('exception_type', '?')}`"),
        "",
        "### Environment",
    ]
    for label, key in (
        ("mATLAS", "tool_version"), ("Python", "python_version"),
        ("Implementation", "python_implementation"), ("git commit", "git_commit"),
    ):
        if md.get(key):
            lines.append(f"- **{label}:** {md[key]}")
    system = (md.get("platform") or {}).get("system")
    if system:
        release = (md.get("platform") or {}).get("release", "")
        lines.append(f"- **Platform:** {system} {release}".rstrip())
    # One report shape, two sources: a crash has a single traceback, a bug report has one
    # stack per live thread. Both reduce to (heading, frames) groups so the sections below
    # are written once — the alternative, a second renderer, is how two formats are born.
    for heading, frames in _frame_groups(safe, is_bug):
        lines += ["", f"### {heading}"]
        for i, frame in enumerate(frames, start=1):
            code = frame.get("code") or ""
            lines.append(
                f"{i}. `{frame.get('file')}:{frame.get('lineno')}` in "
                f"`{frame.get('function')}` — `{code}`")

    lines += [
        "",
        "## Sensitive (review before sharing)",
        "",
        "> Holds run-time values (evidence data, identifiers, argv, environment). Review and "
        "remove this section before sharing the report.",
        "",
    ]
    if not is_bug:
        lines += [f"**Message:** {sensitive.get('exception_message', '')}", ""]
    for heading, frames in _frame_groups(sensitive, is_bug):
        lines += [f"### Locals — {heading}"]
        for frame in frames:
            lines.append(f"#### `{frame.get('function')}` (line {frame.get('lineno')})")
            frame_locals = frame.get("locals") or {}
            if not frame_locals:
                lines.append("- _(no locals)_")
            for name, value in frame_locals.items():
                lines.append(f"- `{name}`: {_md_value(value)}")
            if frame.get("locals_truncated"):
                lines.append("- _(further locals dropped — per-frame byte budget)_")

    # The empty sections that make the file a fileable bug report rather than a raw dump.
    lines += [
        "",
        "## Steps to reproduce",
        "",
        "1. ",
        "",
        "## Expected vs actual",
        "",
        "**Expected:** ",
        "",
        "**Actual:** ",
        "",
        "## Notes",
        "",
    ]
    return "\n".join(lines) + "\n"


def _frame_groups(section: dict[str, Any], is_bug: bool) -> list[tuple[str, list]]:
    """(heading, frames) pairs for one half of a report, whichever kind it is."""
    if is_bug:
        return [(f"Thread {t.get('thread_name')}"
                 + (" (main)" if t.get("is_main") else ""), t.get("frames") or [])
                for t in section.get("threads", [])]
    return [("Stack (crash site last)", section.get("frames", []))]


# --- report assembly + hook -------------------------------------------------

def write_crash_report(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: Any,
    *,
    context: dict[str, Any] | None = None,
) -> Path | None:
    """Write the crash report as a machine ``.json`` plus a human ``.md`` beside it, and
    return the JSON path (or None if disabled/failed).

    The report is split into ``safe`` (shareable) and ``sensitive`` (review before sharing);
    see the module docstring. Guaranteed non-throwing: any internal failure is logged and
    swallowed so the original exception is never masked."""
    try:
        cfg = _diag_settings()
        if not cfg.enabled:
            return None
        frames = _collect_frames(tb, cfg)
        safe, sensitive = _base_sections(cfg, context)
        # The exception TYPE is safe (a class name); its message/args are run-time data.
        safe["exception_type"] = _typename(exc)
        safe["frames"] = [_safe_frame(frame) for frame in frames]
        sensitive |= {
            "exception_message": _anonymise_paths(str(exc), cfg),
            "exception_args": [_summarise(None, arg, cfg, 1) for arg in getattr(exc, "args", ())],
            # __notes__ carries the build_observations breadcrumb (source/table/line/field/value).
            "notes": [_anonymise_paths(str(n), cfg)
                      for n in (getattr(exc, "__notes__", []) or [])] or None,
            "traceback": _anonymise_paths(
                "".join(traceback.format_exception(exc_type, exc, tb)), cfg),
            "frames": [_sensitive_frame(frame) for frame in frames],
        }
        return _emit(_envelope(KIND_ERROR, safe, sensitive),
                     _report_path(cfg, KIND_ERROR))
    except Exception:  # noqa: BLE001 - the handler must never raise over the original error.
        log.exception("Failed to write crash report")
        return None


def write_bug_report(context: dict[str, Any] | None = None) -> Path | None:
    """Write a BUG report — the same artefact as a crash report, with no exception.

    WHY this exists: the crash path only fires when something raises. A bug that produces
    the wrong answer quietly raises nothing, and by the time an operator notices, the state
    that explains it lives only in the running frames. This captures every live thread's
    stack and locals on demand, so "it did the wrong thing" arrives with the same evidence
    an exception would have carried.

    Same schema, same safe/sensitive split and the same Markdown twin as
    ``write_crash_report``, so one reader (and one redaction pass) handles both. Guaranteed
    non-throwing: a failed report returns None rather than taking the app down with it."""
    try:
        cfg = _diag_settings()
        if not cfg.enabled:
            return None
        # Our own frame is skipped so the report describes the application, not the reporter.
        stacks = _collect_thread_stacks(cfg, skip_frame=sys._getframe())
        safe, sensitive = _base_sections(cfg, context)
        safe["exception_type"] = None   # a bug report has no exception, by definition
        safe["threads"] = [
            {key: stack[key] for key in
             ("thread_name", "thread_ident", "daemon", "is_main")}
            | {"frames": [_safe_frame(frame) for frame in stack["frames"]]}
            for stack in stacks
        ]
        sensitive["threads"] = [
            {"thread_name": stack["thread_name"],
             "frames": [_sensitive_frame(frame) for frame in stack["frames"]]}
            for stack in stacks
        ]
        return _emit(_envelope(KIND_BUG, safe, sensitive), _report_path(cfg, KIND_BUG))
    except Exception:  # noqa: BLE001 - filing a bug report must never itself crash the app.
        log.exception("Failed to write bug report")
        return None


def capture_exception(exc: BaseException, *, context: dict[str, Any] | None = None) -> Path | None:
    """Write a crash report for an exception that was CAUGHT rather than left to propagate.

    WHY: ``sys.excepthook`` only sees exceptions nobody handled. Every broad
    ``except Exception: log.exception(...); return`` block — and, in this app, every
    background task, whose runner turns a failure into a signal carrying only a formatted
    string — swallows the exception before the hook can see it. That is the most common
    failure path in the GUI, so without this funnel the reports that matter most are the
    ones never written."""
    return write_crash_report(type(exc), exc, exc.__traceback__, context=context)


def _base_sections(cfg: DiagnosticsSettings,
                   context: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """The (safe, sensitive) parts EVERY report carries, whatever its kind.

    Kept here rather than written out in each writer so the two kinds cannot drift on what
    counts as environment or as sensitive — the whole reason they share one format."""
    safe: dict[str, Any] = {"metadata": _metadata(cfg), "context": context or {}}
    sensitive: dict[str, Any] = {
        # argv can carry operator input (a case subject, an evidence path) — sensitive.
        "argv": [_relativise_path(arg, cfg, basename_fallback=False) for arg in sys.argv],
    }
    if cfg.capture_env:
        # Every value passes through redaction by its NAME, so a matched secret is hashed.
        sensitive["environ"] = {k: _summarise(k, v, cfg, 1) for k, v in os.environ.items()}
    return safe, sensitive


def _envelope(kind: str, safe: dict[str, Any], sensitive: dict[str, Any]) -> dict[str, Any]:
    """Wrap the two halves in the report envelope both kinds share."""
    return {"schema": REPORT_SCHEMA, "tool": "mATLAS", "kind": kind,
            "safe": safe, "sensitive": sensitive}


def _emit(report: dict[str, Any], json_path: Path) -> Path | None:
    """Write the JSON (authoritative) and its best-effort Markdown twin; return the JSON path.

    Shared by both kinds so an error and a bug report can never drift into different files
    on disk — the reason they are one format in the first place."""
    # utf-8-sig so Windows tooling detects the encoding (house encoding rule).
    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8-sig")
    log.error(f"{report.get('kind', 'diagnostic').capitalize()} report (machine) "
              f"written to {json_path}")
    # The human Markdown is a convenience alongside the authoritative JSON: render it under
    # its OWN guard so a rendering slip loses neither the JSON nor the original exception.
    try:
        md_path = json_path.with_suffix(".md")
        md_path.write_text(_render_markdown(report), encoding="utf-8-sig")
        log.error(f"Report (human) written to {md_path}")
    except Exception:  # noqa: BLE001 - the .md is best-effort; the JSON is the artefact.
        log.exception("Failed to write Markdown report")
    return json_path


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
