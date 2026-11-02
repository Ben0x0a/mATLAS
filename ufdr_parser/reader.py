"""Stream a report.xml binary stream into flattened records and source entries.

Defines:    iter_items — a single-pass lxml iterparse generator that yields FlatRecord objects
            (one per ``<model>``) and SourceEntry objects (from ``<extraInfos>``) — and
            iter_block_models, the same for one modelType block.
Used by:    dump (the orchestrator) and parallel (its workers).
Depends on: const, models (build_record), source_lookup (parse_extra_info), integrity
            (ParseAudit), scan (find_decoded_data, ModelTagCounter), lxml.

WHY iterparse + purge: report.xml is up to 32 GB; a full DOM is impossible. We parse
incrementally and free every finished subtree (clear + drop preceding siblings) so memory
stays flat regardless of report size. ``recover=True`` tolerates the invalid XML control
characters Cellebrite reports sometimes contain, replacing the legacy byte-by-byte cleaner; what
it discards is revealed by the independent ModelTagCounter, not by the parser.

RESILIENCE, the reader relies on as little of the report's layout as it can. EVERY ``<model>`` is
a record, wherever it is: the level is simply how many models enclose it, so no container name
(``modelType``, ``modelField``, ``RelatedModels``...) and no depth has to be known in advance. Tags
match in any namespace. A report without ``<decodedData>``, or with models before it, is parsed
whole. Only ``<model>`` itself must exist; if it does not, integrity.check_parse stops the run.

WHY skip taggedFiles: it is the first ~73% of a report and holds the file list, not models (the
per-record source path lives in ``extraInfos``). So we byte-skip to ``<decodedData>`` and parse
from there under a synthetic root that re-declares the namespace, ~2.7x faster than a full parse.
"""
from __future__ import annotations

import io
import logging
from collections import Counter
from typing import BinaryIO, Iterator

from lxml import etree

from ufdr_parser.const import LEVEL_TOP, LOCAL_MODEL, NS
from ufdr_parser.integrity import ParseAudit
from ufdr_parser.models import FlatRecord, build_record
from ufdr_parser.scan import ModelTagCounter, find_decoded_data
from ufdr_parser.source_lookup import SourceEntry, parse_extra_info

log = logging.getLogger(__name__)

Item = FlatRecord | SourceEntry
_DRAIN_CHUNK = 1024 * 1024
# Any namespace: a new report version must not make every tag "unknown".
_ANY_MODEL = "{*}model"
_ANY_EXTRA_INFO = "{*}extraInfo"
_ANY_FILE = "{*}file"
_LOCAL_EXTRA_INFO = "extraInfo"
_LOCAL_FILE = "file"
# Top-level sections that are read (decodedData, extraInfos) or knowingly skipped (the file list).
_HANDLED_SECTIONS = frozenset({"decodedData", "extraInfos", "taggedFiles"})

# Synthetic root so a fragment beginning at <decodedData> is well-formed and namespaced.
# The report's own trailing </project> closes it (we read to EOF).
_SYNTHETIC_ROOT = f'<?xml version="1.0" encoding="utf-8"?><project xmlns="{NS}">'.encode()


# Synthetic wrapper for a single modelType block parsed in isolation by a worker.
_BLOCK_PREFIX = f'<?xml version="1.0" encoding="utf-8"?><decodedData xmlns="{NS}">'.encode()
_BLOCK_SUFFIX = b"</decodedData>"


class ChainedReader:
    """A read()-only stream that serves a prefix buffer, then a backing stream.

    Used to splice the synthetic root + the bytes from ``<decodedData>`` (or ``<extraInfos>``)
    onward in front of the still-open report stream, without buffering the whole report.
    """

    def __init__(self, prefix: bytes, source: BinaryIO) -> None:
        self._prefix: io.BytesIO | None = io.BytesIO(prefix)
        self._source = source

    def read(self, size: int = -1) -> bytes:
        if self._prefix is not None:
            data = self._prefix.read(size)
            if data:
                return data
            self._prefix = None  # prefix exhausted; fall through to the source
        return self._source.read(size)


def extra_infos_stream(seeked_source: BinaryIO) -> ChainedReader:
    """Wrap a stream positioned at ``<extraInfos>`` as a namespaced document to EOF.

    The report's own trailing ``</project>`` closes the synthetic root.
    """
    header = f'<?xml version="1.0" encoding="utf-8"?><project xmlns="{NS}">'.encode()
    return ChainedReader(header, seeked_source)


class BlockRangeReader:
    """A read()-only stream over one modelType block: prefix, then ``length`` bytes from a
    seeked source, then a suffix — so a worker parses its block without buffering it whole.

    Streaming (not slurping) keeps a worker's memory flat even when its block is multi-GB.
    """

    def __init__(self, source: BinaryIO, length: int) -> None:
        self._source = source
        self._remaining = length
        self._prefix: io.BytesIO | None = io.BytesIO(_BLOCK_PREFIX)
        self._suffix: io.BytesIO | None = None  # created once the body is exhausted

    def read(self, size: int = -1) -> bytes:
        if self._prefix is not None:
            data = self._prefix.read(size)
            if data:
                return data
            self._prefix = None
        if self._remaining > 0:
            want = self._remaining if size is None or size < 0 else min(size, self._remaining)
            data = self._source.read(want)
            self._remaining -= len(data)
            if data:
                return data
            self._remaining = 0  # source ended early; fall through to the suffix
        if self._suffix is None:
            self._suffix = io.BytesIO(_BLOCK_SUFFIX)
        return self._suffix.read(size)


def iter_block_models(stream: BinaryIO, audit: ParseAudit | None = None) -> "Iterator[FlatRecord]":
    """Yield a FlatRecord for every model in one wrapped modelType block stream."""
    audit = audit if audit is not None else ParseAudit()
    counted = _ModelTagCountingReader(stream)
    context = etree.iterparse(counted, events=("end",), tag=_ANY_MODEL, recover=True, huge_tree=True)
    for _event, elem in context:
        yield _take_model(elem, audit)
    audit.model_tags += counted.finish()


def _take_model(elem: "etree._Element", audit: ParseAudit) -> FlatRecord:
    """A model's record, at its OWN end tag; the element is then freed.

    WHY at its own end tag: flattening a top-level model's whole subtree when IT ends keeps that
    subtree in memory and frees it in one ``clear()``. For one huge model (a chat with 100,000
    messages) lxml's clear repairs namespace references node by node (moveNodeToDocument /
    _fixCNs) in a way that grows far faster than the subtree: over ten minutes of one core inside
    C, holding the GIL — no progress, no error, and a frozen GUI. Emitting and freeing every model
    as soon as it ends keeps each subtree tiny. A nested row is emitted before its parent's, but
    rows are grouped by (level, type) and stay in document order.
    """
    record = _model_record(elem)
    audit.observe(record)
    _purge(elem)
    return record


class _ModelTagCountingReader:
    """A read()-only pass-through feeding what the parser reads to a ModelTagCounter."""

    def __init__(self, source) -> None:
        self._source = source
        self._counter = ModelTagCounter()

    def read(self, size: int = -1) -> bytes:
        data = self._source.read(size)
        if data:
            self._counter.feed(data)
        return data

    def finish(self) -> int:
        # What the parser never asked for was never fed: read it here, so a parser that stopped
        # early shows up as unaccounted models and not just as a shorter count.
        while chunk := self._source.read(_DRAIN_CHUNK):
            self._counter.feed(chunk)
        return self._counter.finish()


def _model_record(elem: "etree._Element") -> FlatRecord:
    """The record of a model at its end tag: its level and links come from its ancestors.

    HOW: walk up every ancestor; each ``<model>`` met is one level (its element ids are still
    there: only finished elements are freed). The element directly under the nearest enclosing
    model is where this model sits, kept as ``parent_field`` (Position, Parties, RelatedModels...).
    """
    enclosing: list["etree._Element"] = []  # nearest first
    below_nearest = elem
    node = elem.getparent()
    while node is not None:
        if _local(node.tag) == LOCAL_MODEL:
            enclosing.append(node)
        elif not enclosing:
            below_nearest = node
        node = node.getparent()
    if not enclosing:
        return build_record(elem, level=LEVEL_TOP)
    top = enclosing[-1]
    parent_field = None if below_nearest is elem else (below_nearest.get("name") or _local(below_nearest.tag))
    return build_record(
        elem,
        level=len(enclosing),
        parent_id=enclosing[0].get("id"),
        top_type=top.get("type") or "",
        top_id=top.get("id"),
        parent_field=parent_field or "",
    )


def _models_stream(stream: BinaryIO) -> tuple[BinaryIO, bool]:
    """The stream to parse and whether it is the WHOLE report (else from ``<decodedData>``).

    The byte skip is only a shortcut: if there is no ``<decodedData>``, or models before it (which
    the skip would lose), the stream is rewound and everything is parsed, slower but complete.
    """
    log.info("Skipping the report's file list (taggedFiles) to the decoded models")
    before = ModelTagCounter()
    tail, offset = find_decoded_data(stream, before)
    skipped_models = before.finish()
    if tail and not skipped_models:
        log.info(f"Found the decoded models (<decodedData>) at byte {offset:,}; parsing them")
        return ChainedReader(_SYNTHETIC_ROOT + tail, stream), False
    reason = f"{skipped_models} model(s) lie before <decodedData>" if tail else "no <decodedData> section"
    log.warning(f"{reason}: parsing the whole report")
    stream.seek(0)
    return stream, True


def _purge(elem: "etree._Element") -> None:
    """Free a finished element: clear its subtree and drop emptied preceding siblings.

    Without dropping preceding siblings the parent (a <modelType> with tens of thousands
    of models, or <taggedFiles> with hundreds of thousands of files) would retain every
    cleared child and defeat streaming.
    """
    elem.clear()
    parent = elem.getparent()
    if parent is not None:
        while elem.getprevious() is not None:
            del parent[0]


def _local(tag: object) -> str | None:
    if not isinstance(tag, str):
        return None
    return tag.rpartition("}")[2]


def iter_items(stream: BinaryIO, audit: ParseAudit | None = None) -> Iterator[Item]:
    """Yield FlatRecords and SourceEntries from a seekable report stream in document order."""
    audit = audit if audit is not None else ParseAudit()
    models_stream, whole_report = _models_stream(stream)
    decoded = _ModelTagCountingReader(models_stream)
    # Only these end tags are acted on; filtering them in lxml (C) spares a Python loop turn for
    # every other element (each field and value), ~15% of a dump. The other elements are still
    # built, inside the models they belong to.
    tags = (_ANY_MODEL, _ANY_EXTRA_INFO, _ANY_FILE) if whole_report else (_ANY_MODEL, _ANY_EXTRA_INFO)
    context = etree.iterparse(decoded, events=("end",), tag=tags, recover=True, huge_tree=True)
    for _event, elem in context:
        local = _local(elem.tag)
        if local == LOCAL_MODEL:
            yield _take_model(elem, audit)
        elif local == _LOCAL_EXTRA_INFO:
            entry = parse_extra_info(elem)
            if entry is not None:
                yield entry
            _purge(elem)
        elif local == _LOCAL_FILE and not any(_local(a.tag) == LOCAL_MODEL for a in elem.iterancestors()):
            _purge(elem)  # whole-report parse only: the file list must not pile up in memory
    audit.model_tags += decoded.finish()
    note_unread_sections(context.root, audit.unread_sections)


def note_unread_sections(root: "etree._Element | None", sections: Counter) -> None:
    """Count what the report's other top-level sections hold: reported, never dropped quietly."""
    for section in root if root is not None else ():
        local = _local(section.tag)
        if local and local not in _HANDLED_SECTIONS and len(section):
            sections[local] += len(section)
