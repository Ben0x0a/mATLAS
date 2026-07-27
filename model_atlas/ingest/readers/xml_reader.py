"""XML format reader (ingest brick): namespace -> dialect dispatch; the model type is the
subunit.

Defines:    XmlDialect (the per-format contract), the namespace-keyed dialect registry
            (register_dialect / get_dialect / dialects), sniff_namespace, the shared
            SOURCE_COLUMNS, the ``_local`` tag helper, and ``XmlReader`` (format "xml").
Used by:    the concrete dialects in ``xml_specifics`` (which register themselves against
            this module) and the ``format -> Reader`` registry (``XmlReader`` registered
            on import).
Depends on: ``model_atlas.core.vocabularies`` (``RecordState``), ``model_atlas.ingest.
            readers.base``/``registry``, and the standard library only (dialects pull in
            lxml/ufdr_parser, imported lazily so this module stays light).

Ported from ``sources/readers/xml_reader.py``: the dialect Protocol/registry,
``sniff_namespace`` and the per-report prepare/cache shape are kept verbatim. What is
DROPPED (deliberately, not an oversight) is the old reader's staging-tier machinery
(``STAGING_TIER``, ``acquire_source``/``container.finalize``): discover/containers
has not landed, so ``source`` here is already a plain, real path on disk — there is no
container to stage from or copy-tier to choose. A report is always read in place (never
copied) via a fresh seekable stream per pass, exactly as before.

Enumeration order: unlike sqlite/csv/excel, ``core/determinism.py`` states no numbered rule for XML
(the "future UFDR reader" placeholder in docs/refactor_plan.md §2) — a model's records are
numbered in the dialect's own document/block order, which is a deterministic linear parse,
so re-reading the same report yields the same line_no sequence.
"""
from __future__ import annotations

import atexit
import logging
import re
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable, Protocol, runtime_checkable

import pandas as pd

from model_atlas.core.vocabularies import RecordState
from model_atlas.ingest.readers.base import RawRecord
from model_atlas.ingest.readers.registry import register_reader

# A factory returning a fresh, seekable binary stream over the report XML, opened at 0.
# Dialects call it once per pass (scan / source / each block) and seek as needed, so the
# report is read IN PLACE — never copied.
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

    Reads only the head of the document, so it is cheap even for a 32 GB report.
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


@dataclass
class _ReportCache:
    """Per-report state cached across the several model types one report yields."""

    dialect: XmlDialect
    prep: object
    work_dir: Path
    frames: dict[str, tuple[pd.DataFrame, tuple[str, ...]]] = field(default_factory=dict)


@register_reader
class XmlReader:
    """Reader for ``xml``: dispatch by namespace to a dialect, cache per report.

    The report is read in place (never copied — no container/staging brick exists yet);
    the scan + source map are built once per report and reused across its model types.
    """

    format = "xml"

    def __init__(self) -> None:
        self._cache: dict[Path, _ReportCache] = {}
        # Dialects parse via lxml/ufdr_parser; import them lazily so model_atlas core stays
        # free of that dependency until an XML source is actually read.
        self._dialects_loaded = False
        atexit.register(self._cleanup)

    # --- Reader protocol -----------------------------------------------------

    def subunits(self, source: Path) -> list[str]:
        try:
            cache = self._prepare(source)
            return cache.dialect.model_types(cache.prep)
        except Exception:  # noqa: BLE001 - a probe failure must never abort matching
            log.debug(f"xml subunits failed for {source}", exc_info=True)
            return []

    def iter_records(
        self, source: Path, subunit: str | None, params: dict[str, Any] | None = None,
    ) -> Iterator[RawRecord]:
        if not subunit:
            raise ValueError("an xml read needs a model type (subunit)")
        cache = self._prepare(source)
        dataframe, _source_fields = self._frame(cache, subunit)
        for line_no, row in enumerate(dataframe.to_dict(orient="records"), start=1):
            yield RawRecord(line_no=line_no, fields=row, recovery_state=RecordState.LIVE)

    def peek_fields(self, source: Path, subunit: str | None = None) -> set[str] | None:
        if not subunit:
            return None
        try:
            cache = self._prepare(source)
            _dataframe, source_fields = self._frame(cache, subunit)
            return set(source_fields)
        except Exception:  # noqa: BLE001 - a peek failure must never abort matching
            log.debug(f"xml peek_fields failed for {source}", exc_info=True)
            return None

    # --- internals ------------------------------------------------------------

    def _frame(self, cache: _ReportCache, model: str) -> tuple[pd.DataFrame, tuple[str, ...]]:
        if model not in cache.frames:
            cache.frames[model] = cache.dialect.read_model(cache.prep, model)
        return cache.frames[model]

    def _prepare(self, source: Path) -> _ReportCache:
        cached = self._cache.get(source)
        if cached is not None:
            return cached

        open_stream: OpenStream = lambda: source.open("rb")  # noqa: E731 - trivial factory
        with open_stream() as stream:
            head = stream.read(8192)
        namespace = sniff_namespace(head)
        dialect = self._dialect_for(namespace)
        if dialect is None:
            raise ValueError(
                f"{source}: unsupported XML namespace {namespace!r} "
                f"(known: {[d.namespace for d in dialects()]})"
            )

        work_dir = Path(tempfile.mkdtemp(prefix="matlas-xml-"))
        prep = dialect.prepare(open_stream, work_dir)
        cache = _ReportCache(dialect=dialect, prep=prep, work_dir=work_dir)
        self._cache[source] = cache
        log.info(f"Prepared XML report {source} (namespace {namespace})")
        return cache

    def _dialect_for(self, namespace: str | None) -> XmlDialect | None:
        if not self._dialects_loaded:
            # Importing the package registers every dialect (and pulls in lxml/ufdr_parser).
            import model_atlas.ingest.readers.xml_specifics  # noqa: F401
            self._dialects_loaded = True
        return get_dialect(namespace)

    def _cleanup(self) -> None:
        for cache in self._cache.values():
            shutil.rmtree(cache.work_dir, ignore_errors=True)
        self._cache.clear()
