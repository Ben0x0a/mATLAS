"""Stream a report.xml binary stream into flattened records and source entries.

Defines:    iter_items — a single-pass lxml iterparse generator that yields FlatRecord
            objects (from <decodedData>) and SourceEntry objects (from <extraInfos>).
Used by:    dump (the orchestrator).
Depends on: const, models (build_record), source_lookup (parse_extra_info), lxml.

WHY iterparse + purge: report.xml is up to 32 GB; a full DOM is impossible. We parse
incrementally and free every finished subtree (clear + drop preceding siblings) so memory
stays flat regardless of report size. ``recover=True`` tolerates the invalid XML control
characters Cellebrite reports sometimes contain, replacing the legacy byte-by-byte cleaner.
Only top-level models (direct children of a ``<modelType>``) are flattened; nested models
are reached through their parent's subtree, so their end events are skipped here.

WHY skip taggedFiles: the only sections the dumper consumes are ``<decodedData>`` (the
models) and ``<extraInfos>`` (the id->source map). ``<taggedFiles>`` is the first ~73% of
a report yet contributes nothing — the per-record source path lives in ``extraInfos``, not
here. So we byte-skip to ``<decodedData>`` and parse from there (under a synthetic root
that re-declares the namespace), turning a full parse into a ~2.7x faster one. We still
read *through* the taggedFiles bytes to reach decodedData; we just don't build a DOM for
them.
"""
from __future__ import annotations

import io
import logging
from typing import BinaryIO, Iterator

from lxml import etree

from ufdr_parser.const import (
    LEVEL_SUBSUB,
    LEVEL_TOP,
    LOCAL_MODEL,
    LOCAL_MODEL_FIELD,
    LOCAL_MODEL_TYPE,
    LOCAL_MULTI_MODEL_FIELD,
    NS,
    TAG_EXTRA_INFO,
    TAG_MODEL,
)
from ufdr_parser.models import FlatRecord, build_record
from ufdr_parser.scan import find_decoded_data
from ufdr_parser.source_lookup import SourceEntry, parse_extra_info

log = logging.getLogger(__name__)

Item = FlatRecord | SourceEntry

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


def iter_block_models(stream: BinaryIO) -> "Iterator[FlatRecord]":
    """Yield FlatRecords for the models in one wrapped modelType block stream."""
    context = etree.iterparse(stream, events=("end",), recover=True, huge_tree=True)
    for _event, elem in context:
        if elem.tag == TAG_MODEL:
            in_chain, record = _model_record(elem)
            if record is not None:
                yield record
            if in_chain:
                _purge(elem)


_MODEL_CONTAINERS = (LOCAL_MODEL_FIELD, LOCAL_MULTI_MODEL_FIELD)


def _model_record(elem: "etree._Element") -> tuple[bool, FlatRecord | None]:
    """``(in_chain, record)`` for a model at its OWN end tag.

    ``in_chain`` says the model hangs off a top-level model through the decomposition chain
    (so nothing else reads it once its record is built, and it can be freed); ``record`` is
    None when the model is outside the chain or deeper than the decomposition goes.

    HOW: walk up from the model through the ``model -> modelField|multiModelField -> model``
    chain to the top-level model (whose parent is a ``<modelType>``); the chain length is the
    level, the nearest model is the parent and the outermost the top. A model reached any other
    way yields no record and is NOT freed (it stays inside its ancestor, read by nothing, and
    is freed with it, as before); one deeper than LEVEL_SUBSUB is freed without a record —
    exactly the models the recursive ``models.flatten_model`` would visit, same values.
    WHY at the model's own end tag: the alternative — flattening a top-level model's whole
    subtree when IT ends — keeps that subtree in memory and then frees it in one ``clear()``.
    For one huge model (a chat with 100,000 messages) lxml's clear repairs namespace references
    node by node (moveNodeToDocument / _fixCNs) in a way that grows far faster than the
    subtree: over ten minutes of one core inside C, holding the GIL — no progress, no error,
    and a frozen GUI. Emitting and freeing every model as soon as it ends keeps each subtree
    tiny. Row content and per-(level, type) order are unchanged: a nested row is now emitted
    before its parent's, but rows are grouped by (level, type) and stay in document order.
    """
    ancestors: list["etree._Element"] = []  # model ancestors, nearest first
    node = elem
    while True:
        container = node.getparent()
        if container is None:
            return False, None
        local = _local(container.tag)
        if local == LOCAL_MODEL_TYPE:
            break
        if local not in _MODEL_CONTAINERS:
            return False, None
        owner = container.getparent()
        if owner is None or _local(owner.tag) != LOCAL_MODEL:
            return False, None
        ancestors.append(owner)
        node = owner
    level = len(ancestors)
    if level > LEVEL_SUBSUB:
        return True, None  # parity with the three-level decomposition: deeper models are omitted
    if level == LEVEL_TOP:
        return True, build_record(elem, level=LEVEL_TOP)
    top = ancestors[-1]
    return True, build_record(
        elem,
        level=level,
        parent_id=ancestors[0].get("id"),
        top_type=top.get("type") or "",
        top_id=top.get("id"),
    )


def _skip_to_decoded_data(stream: BinaryIO) -> BinaryIO:
    """Return a stream positioned at ``<decodedData>`` under a synthetic namespaced root.

    Reads ``stream`` forward (CDATA-aware byte scan, no XML build) via the shared
    ``find_decoded_data`` until the real decodedData element, then chains the synthetic root +
    the marker-onward bytes ahead of the rest of stream. Sharing the finder with the scanner
    means a literal ``<decodedData`` inside skipped taggedFiles CDATA cannot mis-anchor here.
    """
    log.info("Skipping the report's file list (taggedFiles) to the decoded models")
    tail, offset = find_decoded_data(stream)
    if not tail:
        # No decodedData (e.g. an empty/odd report): yield a valid, empty document.
        log.warning(f"no <decodedData> section found in {offset:,} byte(s); nothing to parse")
        return ChainedReader(_SYNTHETIC_ROOT + b"</project>", stream)
    log.info(f"Found the decoded models (<decodedData>) at byte {offset:,}; parsing them")
    return ChainedReader(_SYNTHETIC_ROOT + tail, stream)


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


def iter_items(stream: BinaryIO) -> Iterator[Item]:
    """Yield FlatRecords and SourceEntries from a report.xml stream in document order.

    Parsing starts at ``<decodedData>`` (taggedFiles is byte-skipped), so only model and
    extraInfo end events are seen.
    """
    decoded = _skip_to_decoded_data(stream)
    context = etree.iterparse(decoded, events=("end",), recover=True, huge_tree=True)
    for _event, elem in context:
        tag = elem.tag
        if tag == TAG_MODEL:
            # Every model is emitted (and freed) at its own end tag; see _model_record.
            in_chain, record = _model_record(elem)
            if record is not None:
                yield record
            if in_chain:
                _purge(elem)
        elif tag == TAG_EXTRA_INFO:
            entry = parse_extra_info(elem)
            if entry is not None:
                yield entry
            _purge(elem)
