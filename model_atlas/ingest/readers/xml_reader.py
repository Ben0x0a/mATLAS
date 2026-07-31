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

Ported from ``sources/readers/xml_reader.py``: the dialect Protocol/registry and the
per-report prepare/cache shape are kept verbatim. ``sniff_namespace`` is now ROOT-specific
(it reads the default xmlns of the root element, not the first element that carries one) and
the reader grows its read window until the root start-tag is complete, so a root whose xmlns
sits far into the document is not missed (which would drop the whole report). What is
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

# The DEFAULT namespace attribute (``xmlns="..."``) — NOT ``xmlns:prefix``, which declares a
# prefixed namespace rather than the element's own default. Applied to the ROOT start-tag only.
_XMLNS_DEFAULT_ATTR = re.compile(rb'\bxmlns\s*=\s*"([^"]+)"')

# Grow the sniff window up to this bound while hunting the complete root start-tag; read it in
# these chunks. WHY grow rather than a fixed 8 KB head: a root whose xmlns sits past the first
# 8 KB (many namespace attributes, a large DOCTYPE) would otherwise sniff as None and the whole
# report would be dropped.
_ROOT_TAG_SEARCH_CAP = 1024 * 1024
_ROOT_TAG_READ_CHUNK = 64 * 1024


def _local(tag: object) -> str | None:
    """Local name of an lxml element tag, or None for comments / processing instructions."""
    return tag.rpartition("}")[2] if isinstance(tag, str) else None


def _tag_end(data: bytes, start: int) -> int:
    """Index of the ``>`` that closes the start-tag beginning at ``start``, or -1 when the tag
    is not yet complete in ``data``. A ``>`` inside a quoted attribute value is skipped, so an
    attribute value containing ``>`` does not truncate the tag."""
    quote: bytes | None = None
    for j in range(start, len(data)):
        char = data[j:j + 1]
        if quote is not None:
            if char == quote:
                quote = None
        elif char in (b'"', b"'"):
            quote = char
        elif char == b">":
            return j
    return -1


def _root_start_tag(data: bytes) -> bytes | None:
    """The ROOT element's opening tag bytes (``<name ...>``), or None when the prolog/root tag
    is not yet fully present (or ``data`` is not XML).

    HOW: skip the prolog — the XML declaration / processing instructions (``<? .. ?>``),
    comments (``<!-- .. -->``) and declarations/DOCTYPE (``<! .. >``) — then return the first
    real element's start-tag up to its closing ``>``. WHY the root specifically: the namespace
    that discriminates the dialect is the ROOT's default xmlns, and picking the first element
    that merely happens to carry an xmlns could select a CHILD's — the wrong dialect."""
    index, size = 0, len(data)
    while index < size:
        while index < size and data[index:index + 1].isspace():
            index += 1
        if index >= size:
            return None
        if data[index:index + 1] != b"<":
            return None  # a non-markup first byte: not XML
        if data.startswith(b"<?", index):          # <?xml ...?> or a processing instruction
            end = data.find(b"?>", index)
            if end == -1:
                return None
            index = end + 2
            continue
        if data.startswith(b"<!--", index):        # a comment
            end = data.find(b"-->", index)
            if end == -1:
                return None
            index = end + 3
            continue
        if data.startswith(b"<!", index):          # a DOCTYPE / other declaration
            end = data.find(b">", index)
            if end == -1:
                return None
            index = end + 1
            continue
        end = _tag_end(data, index)                # the root element's own start-tag
        return data[index:end + 1] if end != -1 else None
    return None


def sniff_namespace(head: bytes) -> str | None:
    """The default namespace declared on the ROOT element in ``head``, or None.

    Root-specific (never the first element that happens to carry an xmlns), so a root without a
    namespace is not mis-typed from a namespaced child. Operates on whatever bytes it is given
    — the reader grows that window until the root tag is complete (see ``_sniff_root_namespace``).
    """
    tag = _root_start_tag(head)
    if tag is None:
        return None
    match = _XMLNS_DEFAULT_ATTR.search(tag)
    return match.group(1).decode("utf-8", "replace") if match else None


def _sniff_root_namespace(open_stream: OpenStream) -> str | None:
    """Grow the read window until the ROOT start-tag is complete, then sniff its default
    namespace. Bounded by ``_ROOT_TAG_SEARCH_CAP`` so a report with no complete root tag (or a
    non-XML file) cannot read unboundedly."""
    with open_stream() as stream:
        buffer = b""
        while len(buffer) < _ROOT_TAG_SEARCH_CAP:
            chunk = stream.read(_ROOT_TAG_READ_CHUNK)
            if not chunk:
                break
            buffer += chunk
            if _root_start_tag(buffer) is not None:
                return sniff_namespace(buffer)
        return sniff_namespace(buffer)


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
        namespace = _sniff_root_namespace(open_stream)
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
