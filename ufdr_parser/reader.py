"""Stream a report.xml binary stream into flattened records and source entries.

Defines:    iter_items — a single-pass lxml iterparse generator that yields FlatRecord
            objects (from <decodedData>) and SourceEntry objects (from <extraInfos>).
Used by:    dump (the orchestrator).
Depends on: const, models (flatten_model), source_lookup (parse_extra_info), lxml.

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
    LEVEL_TOP,
    LOCAL_MODEL_TYPE,
    NS,
    TAG_EXTRA_INFO,
    TAG_MODEL,
)
from ufdr_parser.models import FlatRecord, flatten_model
from ufdr_parser.source_lookup import SourceEntry, parse_extra_info

log = logging.getLogger(__name__)

Item = FlatRecord | SourceEntry

# Section we start parsing at; everything before it (header + taggedFiles) is skipped.
_DECODED_MARKER = b"<decodedData"
_CHUNK = 8 * 1024 * 1024
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
    """Yield FlatRecords for the top-level models in one wrapped modelType block stream."""
    context = etree.iterparse(stream, events=("end",), recover=True, huge_tree=True)
    for _event, elem in context:
        if elem.tag != TAG_MODEL:
            continue
        parent = elem.getparent()
        if parent is not None and _local(parent.tag) == LOCAL_MODEL_TYPE:
            yield from flatten_model(elem, level=LEVEL_TOP)
            _purge(elem)


def _skip_to_decoded_data(stream: BinaryIO) -> BinaryIO:
    """Return a stream positioned at ``<decodedData>`` under a synthetic namespaced root.

    Reads ``stream`` forward (cheap byte scan, no XML build) until the decodedData marker,
    then chains the synthetic root + the marker-onward bytes ahead of the rest of stream.
    """
    carry = b""
    overlap = len(_DECODED_MARKER) - 1
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            # No decodedData (e.g. an empty/odd report): yield a valid, empty document.
            log.warning("no <decodedData> section found; nothing to parse")
            return ChainedReader(_SYNTHETIC_ROOT + b"</project>", stream)
        data = carry + chunk
        index = data.find(_DECODED_MARKER)
        if index != -1:
            return ChainedReader(_SYNTHETIC_ROOT + data[index:], stream)
        carry = data[-overlap:]


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
            parent = elem.getparent()
            # Only flatten top-level models; nested models are handled within the
            # parent's subtree, so their (earlier) end events are ignored here.
            if parent is not None and _local(parent.tag) == LOCAL_MODEL_TYPE:
                yield from flatten_model(elem, level=LEVEL_TOP)
                _purge(elem)
        elif tag == TAG_EXTRA_INFO:
            entry = parse_extra_info(elem)
            if entry is not None:
                yield entry
            _purge(elem)
