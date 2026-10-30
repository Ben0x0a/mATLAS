"""Locate the decoded models in a report by byte scanning: the ``<decodedData>`` start, each
``<modelType>`` block's byte range, and the trailing ``<extraInfos>``.

Defines:    find_decoded_data (skip the header + taggedFiles), ModelTypeBlock, DecodedIndex and
            index_model_types (one forward scan for parallel parsing).
Used by:    reader (skip to the models), parallel (one block per worker) and the model_atlas
            Cellebrite XML dialect.
Depends on: standard library only.

WHY a byte scan (not a parse): only offsets are needed, so a byte walk is far cheaper than
building any DOM. ``<modelType>`` blocks do not nest, so a block ends where the next begins
(the last ends at ``</decodedData>``).

CDATA SAFETY: a field value can contain the literal text ``<modelType`` or ``<decodedData``
inside ``<![CDATA[ ... ]]>``; taking it for the real element would anchor the parse at the
wrong offset and silently corrupt every record. Each candidate marker is therefore checked
with ``_inside_cdata_before``.

HOW it stays fast: markers are found with ``bytes.find`` (C speed) and only a CANDIDATE is
checked, by looking back to the nearest ``<![CDATA[`` and seeing whether a ``]]>`` closed it
before the candidate. That is exact because outside CDATA, ``<![CDATA[`` can only be a real
CDATA start (a bare ``<`` is not allowed in XML text or attribute values), while inside CDATA
it is plain text — and a CDATA section always ends at its FIRST ``]]>``. WHY not walk every
delimiter: the skipped taggedFiles region and decodedData hold millions of CDATA sections,
and visiting each one in Python made this scan ~30% of a dump (11 s of 41 s on a 1.36 GB
report); the candidate check touches a handful of bytes per real marker instead.

CHUNKING: the report is read in ``_CHUNK`` pieces. The CDATA state is carried at a cut point
as "after every delimiter that STARTS before the cut"; the bytes after the cut (``_CARRY`` of
them, more than any token is long) are re-read with the next chunk, so a token split by the
chunk boundary is always seen whole, exactly once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import BinaryIO

_CHUNK = 8 * 1024 * 1024
_CDATA_OPEN = b"<![CDATA["
_CDATA_CLOSE = b"]]>"
_DECODED_OPEN = b"<decodedData"
_DECODED_CLOSE = b"</decodedData>"
_MODEL_TYPE_OPEN = b"<modelType"
_EXTRA_INFOS_OPEN = b"<extraInfos"
# Bytes kept from one chunk to the next: longer than any token above, so a token starting
# before the cut is complete in the current buffer.
_CARRY = 64
_TYPE_ATTR = re.compile(rb'\btype="([^"]*)"')


def _inside_cdata_before(data: bytes, pos: int, inside_at_start: bool) -> bool:
    """Whether ``pos`` lies inside a CDATA section, after every delimiter STARTING before it.

    ``inside_at_start`` is the state at ``data[0]``. Every token starting before ``pos`` must be
    complete in ``data`` (the callers keep ``_CARRY`` bytes past any position they ask about).
    """
    # The last <![CDATA[ starting before pos (rfind's end bound lets it extend past pos).
    last_open = data.rfind(_CDATA_OPEN, 0, pos + len(_CDATA_OPEN) - 1)
    if last_open != -1:
        # Inside unless a ]]> starting before pos closed it.
        return data.find(_CDATA_CLOSE, last_open + len(_CDATA_OPEN), pos + len(_CDATA_CLOSE) - 1) == -1
    return inside_at_start and data.find(_CDATA_CLOSE, 0, pos + len(_CDATA_CLOSE) - 1) == -1


def find_decoded_data(stream: BinaryIO) -> tuple[bytes, int]:
    """Read ``stream`` from its position to the real ``<decodedData`` element start.

    Returns ``(tail, offset)``: ``tail`` is the already-read buffer from the marker onward (the
    marker plus whatever trailing bytes arrived in the same chunk) and ``offset`` is the
    marker's absolute byte offset. When no decodedData is found, returns ``(b"", bytes_read)``.
    Shared by ``index_model_types`` here and the reader's ``_skip_to_decoded_data`` so both
    locate the section by one definition.
    """
    carry = b""
    base = 0  # absolute offset of carry[0]
    inside = False  # CDATA state at carry[0]
    while True:
        chunk = stream.read(_CHUNK)
        data = carry + chunk
        limit = len(data) if not chunk else max(0, len(data) - _CARRY)
        pos = data.find(_DECODED_OPEN)
        while pos != -1 and pos < limit:
            if not _inside_cdata_before(data, pos, inside):
                return data[pos:], base + pos
            pos = data.find(_DECODED_OPEN, pos + 1)
        if not chunk:
            return b"", base + len(data)
        inside = _inside_cdata_before(data, limit, inside)
        carry = data[limit:]
        base += limit


@dataclass(frozen=True)
class ModelTypeBlock:
    """One ``<modelType type=...>`` block as a byte range in the report XML."""

    model_type: str
    start: int          # offset of the '<' of '<modelType'
    end: int            # offset just past the block (next block start, or </decodedData>)


@dataclass(frozen=True)
class DecodedIndex:
    """Where the decoded models and the source map live in the report XML."""

    blocks: tuple[ModelTypeBlock, ...]
    decoded_end: int
    extra_info_start: int | None


def index_model_types(stream: BinaryIO) -> DecodedIndex:
    """Scan ``stream`` once and return the modelType block ranges + extraInfos start.

    The stream is read from the beginning to EOF; offsets are absolute in the report XML.
    """
    opens: list[tuple[str, int]] = []
    decoded_end: int | None = None
    extra_start: int | None = None

    tail, decoded_start = find_decoded_data(stream)
    if not tail:
        # No decodedData (empty/odd report): nothing to index.
        return DecodedIndex(blocks=(), decoded_end=decoded_start, extra_info_start=None)

    data = tail          # already holds the bytes from the <decodedData marker onward
    base = decoded_start  # absolute offset of data[0]
    inside = False       # the real <decodedData is outside CDATA, so is data[0]
    while True:
        chunk = stream.read(_CHUNK)
        data += chunk
        limit = len(data) if not chunk else max(0, len(data) - _CARRY)
        candidates = sorted(
            (pos, marker)
            for marker in (_MODEL_TYPE_OPEN, _DECODED_CLOSE, _EXTRA_INFOS_OPEN)
            for pos in _positions(data, marker, limit)
        )
        for pos, marker in candidates:
            if _inside_cdata_before(data, pos, inside):
                continue
            if marker == _MODEL_TYPE_OPEN:
                close = data.find(b">", pos)
                if close == -1:
                    if chunk:
                        # The open tag straddles the chunk end: cut before it, see it whole next.
                        limit = pos
                        break
                    close = len(data) - 1  # truncated report: take what is there
                attr = _TYPE_ATTR.search(data, pos, close + 1)
                opens.append((attr.group(1).decode("utf-8", "replace") if attr else "", base + pos))
            elif marker == _DECODED_CLOSE:
                if decoded_end is None:
                    decoded_end = base + pos
            elif extra_start is None:
                extra_start = base + pos
        if not chunk:
            break
        inside = _inside_cdata_before(data, limit, inside)
        data = data[limit:]
        base += limit

    if decoded_end is None:
        decoded_end = base + len(data)
    blocks = [
        ModelTypeBlock(model_type=model_type, start=start, end=opens[i + 1][1] if i + 1 < len(opens) else decoded_end)
        for i, (model_type, start) in enumerate(opens)
    ]
    return DecodedIndex(blocks=tuple(blocks), decoded_end=decoded_end, extra_info_start=extra_start)


def _positions(data: bytes, marker: bytes, limit: int):
    """Every start of ``marker`` in ``data`` before ``limit``."""
    pos = data.find(marker)
    while pos != -1 and pos < limit:
        yield pos
        pos = data.find(marker, pos + 1)
