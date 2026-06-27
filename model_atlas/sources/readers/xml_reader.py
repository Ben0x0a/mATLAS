"""Generic XML input: namespace -> dialect dispatch, shared helpers, and the registry.

Defines:    XmlDialect (the per-format contract), the namespace-keyed dialect registry
            (register_dialect / get_dialect / dialects), sniff_namespace, the shared
            SOURCE_COLUMNS, and the ``_local`` tag helper.
Used by:    the concrete dialects in ``xml_specifics`` (which register themselves) and,
            later, the XmlReader that plugs into the FormatReader registry.
Depends on: pandas (DataFrame typing) and the standard library only.

WHY a registry keyed by namespace: there is a variety of forensic XML (Cellebrite UFED
``report/2.0``, Magnet AXIOM, …). Each is recognised by its root-element namespace; a
preset's ``input_selector`` names that namespace, and the matching dialect knows how to turn
one model type into a DataFrame. New formats are added as a file under ``xml_specifics``
that implements ``XmlDialect`` and calls ``register_dialect`` — no change to this module.
"""
from __future__ import annotations

import atexit
import hashlib
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Protocol, runtime_checkable

import pandas as pd

from model_atlas.sources.container import acquire_source
from model_atlas.sources.readers.base import RECOVERY_LIVE, ReadResult, register_reader
from model_atlas.sources.staging import STAGING_TIER, should_copy

if TYPE_CHECKING:
    from model_atlas.sources.container import SourceFile

# A factory returning a fresh, seekable binary stream over the report XML, opened at 0.
# Dialects call it once per pass (scan / source / each block) and seek as needed, so the
# report is read IN PLACE — never copied — whether it is a loose .xml or a zip entry.
OpenStream = Callable[[], BinaryIO]

log = logging.getLogger(__name__)

# Provenance columns every dialect appends after a model's own fields (filled from whatever
# source-of-record the dialect can resolve; absent ones stay blank).
SOURCE_COLUMNS: tuple[str, ...] = (
    "source_path",
    "source_name",
    "source_table",
    "source_offset",
)

_XMLNS_DEFAULT = re.compile(rb'<\w[\w:.-]*\b[^>]*?\bxmlns\s*=\s*"([^"]+)"', re.DOTALL)


def _local(tag: object) -> str | None:
    """Local name of an lxml element tag, or None for comments / processing instructions."""
    return tag.rpartition("}")[2] if isinstance(tag, str) else None


def sniff_namespace(head: bytes) -> str | None:
    """Return the default namespace declared on the first element, or None.

    Reads only the head of the document, so it is cheap even for a 32 GB report. Used as the
    XML-dialect discriminator (a preset selects an XML source by this namespace).
    """
    match = _XMLNS_DEFAULT.search(head)
    return match.group(1).decode("utf-8", "replace") if match else None


@runtime_checkable
class XmlDialect(Protocol):
    """How one XML format is parsed into per-model-type DataFrames.

    ``prepare`` does the once-per-report work (e.g. an index scan and a source map) and
    returns an opaque handle the other calls reuse, so the caller can cache it across the
    several model types a report yields.
    """

    namespace: str

    def prepare(self, open_stream: OpenStream, work_dir: Path) -> object: ...
    def model_types(self, prep: object) -> list[str]: ...
    def read_model(self, prep: object, model_type: str) -> tuple[pd.DataFrame, tuple[str, ...]]: ...


_DIALECTS: dict[str, XmlDialect] = {}


def register_dialect(dialect: XmlDialect) -> XmlDialect:
    """Register an XML dialect under its namespace (first registration wins)."""
    _DIALECTS.setdefault(dialect.namespace, dialect)
    log.debug(f"Registered XML dialect for namespace {dialect.namespace!r}")
    return dialect


def get_dialect(namespace: str | None) -> XmlDialect | None:
    """The dialect registered for ``namespace``, or None when unsupported/unknown."""
    return _DIALECTS.get(namespace) if namespace is not None else None


def dialects() -> tuple[XmlDialect, ...]:
    return tuple(_DIALECTS.values())


# --------------------------------------------------------------------------- the reader

_CHUNK = 8 * 1024 * 1024


@dataclass
class _ReportCache:
    """Per-report state cached across the several model types one report yields."""

    dialect: XmlDialect
    prep: object
    work_dir: Path
    fingerprint: str
    integrity: dict[str, Any]
    staged_temp: Path | None  # the report copy's temp dir to clean up (None when in place)
    frames: dict[str, tuple[pd.DataFrame, tuple[str, ...]]] = field(default_factory=dict)


@register_reader
class XmlReader:
    """FormatReader for ``xml``: dispatch by namespace to a dialect, cache per report.

    Staging follows the shared tier policy (``STAGING_TIER``): a primary-tier report is
    copied to a temp dir and read from the copy like any primary evidence; a secondary-tier
    report (e.g. a tool's UFDR/AXIOM export — these declare ``tier: secondary``) is read in
    place, so a multi-GB report is not copied. The scan + source map are built once per
    report and reused across its model types.
    """

    format = "xml"
    staging_mode = STAGING_TIER

    def __init__(self) -> None:
        # Keyed by (physical source, copied?) so an in-place peek and a staged read of the
        # same report never clash.
        self._cache: dict[tuple[str, bool], _ReportCache] = {}
        # Dialects parse via lxml/ufdr_parser; import them lazily so model_atlas core stays
        # free of that dependency until an XML source is actually read.
        self._dialects_loaded = False
        atexit.register(self._cleanup)

    # --- FormatReader protocol ---------------------------------------------

    def read(self, file: "SourceFile", params: dict) -> ReadResult:
        model = params.get("model")
        if not model:
            raise ValueError("an xml input_selector must define 'model'")
        copy = should_copy(self.staging_mode, params.get("_tier"))
        cache = self._prepare(file, copy=copy)
        dataframe, source_columns = self._frame(cache, model)
        return ReadResult(
            dataframe=dataframe,
            source_columns=source_columns,
            recovery_state=(RECOVERY_LIVE,) * len(dataframe),
            metadata={
                "format": "xml",
                "namespace": cache.dialect.namespace,
                "model": model,
                "source_fingerprint": cache.fingerprint,
                "row_count": len(dataframe),
                "integrity": cache.integrity,
            },
        )

    def peek_columns(self, file: "SourceFile", selector: Any = None) -> set[str] | None:
        model = getattr(selector, "model", None) if selector is not None else None
        if not model:
            return None
        try:
            # Peeks are read-only probes during matching: read in place (never copy).
            cache = self._prepare(file, copy=False)
            _dataframe, source_columns = self._frame(cache, model)
            return set(source_columns)
        except Exception:  # noqa: BLE001 - a peek failure must never abort matching
            log.debug(f"xml peek_columns failed for {file.logical_path}", exc_info=True)
            return None

    def list_subtables(self, file: "SourceFile") -> list[str]:
        try:
            cache = self._prepare(file, copy=False)
            return cache.dialect.model_types(cache.prep)
        except Exception:  # noqa: BLE001
            log.debug(f"xml list_subtables failed for {file.logical_path}", exc_info=True)
            return []

    # --- internals ----------------------------------------------------------

    def _frame(self, cache: _ReportCache, model: str) -> tuple[pd.DataFrame, tuple[str, ...]]:
        if model not in cache.frames:
            cache.frames[model] = cache.dialect.read_model(cache.prep, model)
        return cache.frames[model]

    @staticmethod
    def _cache_key(file: "SourceFile") -> str:
        # Key on the physical source so two same-named reports (e.g. report.xml in different
        # folders/archives) never share a cache entry. ondisk_path is unique for a loose
        # file; a zip entry has none, so fall back to the archive path + inner path.
        disk = file.container.ondisk_path(file)
        if disk is not None:
            return str(disk)
        archive = getattr(file.container, "path", file.container.root_label)
        return f"{archive}::{file.logical_path}"

    def _prepare(self, file: "SourceFile", *, copy: bool) -> _ReportCache:
        key = (self._cache_key(file), copy)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        container = file.container
        if copy:
            # Primary tier: stage a throwaway copy and read from it (verify original after).
            staged = acquire_source(container, file, copy=True)
            container.finalize(staged)
            report_path = staged.path
            open_stream: Callable[[], BinaryIO] = lambda: report_path.open("rb")
            fingerprint, integrity, staged_temp = staged.fingerprint, staged.integrity, staged.temp_dir
        else:
            open_stream = lambda: container.open(file)
            fingerprint, integrity, staged_temp = None, None, None

        with open_stream() as stream:
            head = stream.read(8192)
        namespace = sniff_namespace(head)
        dialect = self._dialect_for(namespace)
        if dialect is None:
            raise ValueError(
                f"{file.logical_path}: unsupported XML namespace {namespace!r} "
                f"(known: {[d.namespace for d in dialects()]})"
            )

        work_dir = Path(tempfile.mkdtemp(prefix="matlas-xml-"))
        prep = dialect.prepare(open_stream, work_dir)
        if not copy:
            fingerprint = self._fingerprint(open_stream)
            integrity = {"mode": "in_place", "ok": True, "content_sha256": fingerprint}
        cache = _ReportCache(
            dialect=dialect, prep=prep, work_dir=work_dir,
            fingerprint=fingerprint, integrity=integrity, staged_temp=staged_temp,
        )
        self._cache[key] = cache
        log.info(
            f"Prepared XML report {file.logical_path} (namespace {namespace}, "
            f"{'staged copy' if copy else 'in place'})"
        )
        return cache

    def _dialect_for(self, namespace: str | None) -> XmlDialect | None:
        if not self._dialects_loaded:
            # Importing the package registers every dialect (and pulls in lxml/ufdr_parser).
            import model_atlas.sources.readers.xml_specifics  # noqa: F401
            self._dialects_loaded = True
        return get_dialect(namespace)

    @staticmethod
    def _fingerprint(open_stream: Callable[[], BinaryIO]) -> str:
        """Content SHA-256 of the report, hashed in place (one streaming pass, no copy)."""
        digest = hashlib.sha256()
        with open_stream() as stream:
            for chunk in iter(lambda: stream.read(_CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _cleanup(self) -> None:
        for cache in self._cache.values():
            shutil.rmtree(cache.work_dir, ignore_errors=True)
            if cache.staged_temp is not None:
                shutil.rmtree(cache.staged_temp, ignore_errors=True)
        self._cache.clear()
