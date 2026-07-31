"""Index the byte ranges of each ``<modelType>`` block for parallel parsing.

Defines:    ModelTypeBlock, DecodedIndex, and index_model_types — one forward byte scan
            that locates the decodedData region, every ``<modelType>`` block within it, and
            the start of the trailing ``<extraInfos>``.
Used by:    parallel (to hand each worker one block's byte range) and dump.
Depends on: const (NS marker text), standard library only.

WHY a byte scan (not a parse): we only need offsets, so a cheap CDATA-aware byte walk is
far faster than building any DOM and reads the report once. ``<modelType>`` blocks do not
nest, so a block ends where the next one begins (the last ends at ``</decodedData>``). The
scan ignores markers inside ``<![CDATA[ ... ]]>`` so a field value that happens to contain
the literal text ``<modelType`` cannot create a phantom block.

WHY one regex: decodedData holds ~1 CDATA section per field (millions of them), so probing
each marker with separate ``bytes.find`` calls is quadratic and hangs. A single compiled
alternation scanned by ``re.finditer`` finds every marker in one C-level pass; the Python
loop then only walks the (already located) match list.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import BinaryIO

_CHUNK = 8 * 1024 * 1024
_DECODED_CLOSE = b"</decodedData>"
# All structural markers of interest, found in one pass. Order in the class is irrelevant;
# finditer returns them in document order.
_MARKERS = re.compile(
    rb"<!\[CDATA\[|\]\]>|<modelType|</decodedData>|<extraInfos|<decodedData"
)
# Keep this many trailing bytes between chunks so a marker split across the boundary is
# re-seen; large enough to also cover a short ``<modelType ...>`` open tag's start.
_CARRY = 64
_TYPE_ATTR = re.compile(rb'\btype="([^"]*)"')

# CDATA-aware skip of everything before <decodedData> (header + taggedFiles). Only these
# three tokens matter while hunting the section start.
_CDATA_OPEN = b"<![CDATA["
_CDATA_CLOSE = b"]]>"
_SKIP_MARKERS = re.compile(rb"<!\[CDATA\[|\]\]>|<decodedData")
# Enough trailing bytes to re-see a boundary-split ``<decodedData`` or CDATA delimiter.
_SKIP_CARRY = 16


def _scan_for_decoded(data: bytes, in_cdata: bool) -> tuple[int, bool, int]:
    """Find the first ``<decodedData`` in ``data`` that lies OUTSIDE a CDATA section.

    Returns ``(hit, in_cdata, consumed)``: ``hit`` is the index of a genuine ``<decodedData``
    (or -1 if none is confirmed in this buffer); ``in_cdata`` is the CDATA state after
    classifying ``data[:consumed]``; ``consumed`` is how many leading bytes are fully
    classified, so the caller carries ``data[consumed:]`` into the next chunk.

    HOW: walk the interleaved CDATA-open / CDATA-close / decodedData tokens in document order,
    flipping ``in_cdata`` on each delimiter; the first decodedData seen outside CDATA wins.
    WHY: the skipped taggedFiles region is scanned by a cheap plain byte find in the legacy
    code, so a literal ``<decodedData`` inside a CDATA section (or attribute text) there would
    be mistaken for the real element and anchor the whole parse at the wrong offset — silently
    corrupting every record. Advancing ``consumed`` past classified bytes (never re-scanning
    them) keeps this linear over the marker-sparse taggedFiles region.
    """
    consumed = 0
    for match in _SKIP_MARKERS.finditer(data):
        token = match.group()
        if in_cdata:
            if token == _CDATA_CLOSE:
                in_cdata = False
                consumed = match.end()
            continue
        if token == _CDATA_OPEN:
            in_cdata = True
            consumed = match.end()
        elif token == _CDATA_CLOSE:
            consumed = match.end()  # a stray ]]> outside CDATA: ignore, keep advancing
        else:  # <decodedData outside CDATA — the real anchor
            return match.start(), in_cdata, match.start()
    # No hit: keep only a small tail (a boundary-split token is re-seen next chunk); never
    # re-scan already-classified bytes, or the marker-sparse taggedFiles scan turns O(n^2).
    consumed = max(consumed, len(data) - _SKIP_CARRY)
    return -1, in_cdata, consumed


def find_decoded_data(stream: BinaryIO) -> tuple[bytes, int]:
    """Read ``stream`` from its position to the real ``<decodedData`` element start.

    Returns ``(tail, offset)``: ``tail`` is the already-read buffer from the marker onward (the
    marker plus whatever trailing bytes arrived in the same chunk) and ``offset`` is the
    marker's absolute byte offset. When no decodedData is found, returns ``(b"", bytes_read)``.
    CDATA-aware (see ``_scan_for_decoded``), so a marker-like string in the skipped region
    cannot mis-anchor parsing. Shared by ``index_model_types`` here and the reader's
    ``_skip_to_decoded_data`` so both locate the section by one definition.
    """
    carry = b""
    base = 0
    in_cdata = False
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            return b"", base + len(carry)
        data = carry + chunk
        hit, in_cdata, consumed = _scan_for_decoded(data, in_cdata)
        if hit != -1:
            return data[hit:], base + hit
        carry = data[consumed:]
        base += consumed


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
    # Raw (type, start_offset) opens, plus the structural offsets, gathered first; block
    # ends are filled in afterwards from the next start.
    opens: list[tuple[str, int]] = []
    decoded_end: int | None = None
    extra_start: int | None = None

    # CDATA-aware skip of the header + taggedFiles to the real <decodedData> element. WHY not a
    # plain byte find: a literal ``<decodedData`` inside taggedFiles CDATA/attribute text would
    # otherwise mis-anchor the whole scan (see find_decoded_data / _scan_for_decoded).
    tail, decoded_start = find_decoded_data(stream)
    if not tail:
        # No decodedData (empty/odd report): nothing to index.
        return DecodedIndex(blocks=(), decoded_end=decoded_start, extra_info_start=None)

    carry = tail  # already holds the bytes from the <decodedData marker onward
    base = decoded_start  # absolute offset of carry[0]
    in_cdata = False
    first_pass = True  # the seeded tail is the first buffer; do not read before using it

    while True:
        if first_pass:
            chunk = b""
            first_pass = False
        else:
            chunk = stream.read(_CHUNK)
            if not chunk:
                break
        data = carry + chunk
        last_consumed = 0  # end offset (in data) of the last marker we fully handled
        deferred = False
        for match in _MARKERS.finditer(data):
            tok = match.group()
            pos = match.start()
            if in_cdata:
                # Inside CDATA nothing matters except the closing ]]>.
                if tok == b"]]>":
                    in_cdata = False
                    last_consumed = match.end()
                continue
            if tok == b"<![CDATA[":
                in_cdata = True
                last_consumed = match.end()
            elif tok == b"</decodedData>":
                decoded_end = base + pos
                last_consumed = match.end()
            elif tok == b"<extraInfos":
                if extra_start is None:
                    extra_start = base + pos
                last_consumed = match.end()
            elif tok == b"<decodedData":
                last_consumed = match.end()
            elif tok == b"<modelType":
                close = data.find(b">", pos)
                if close == -1:
                    # Open tag straddles the chunk boundary; reprocess it next read.
                    deferred = True
                    break
                attr = _TYPE_ATTR.search(data, pos, close + 1)
                opens.append((attr.group(1).decode("utf-8", "replace") if attr else "", base + pos))
                last_consumed = close + 1
            # ]]> outside CDATA, or <decodedData>'s close handled above: ignore otherwise.
        # Carry from the start of a deferred tag, else a small window so a boundary-split
        # marker is re-seen. While inside CDATA we still only need the small window (the
        # closing ]]> will be found next chunk).
        if deferred:
            keep_from = pos  # the unfinished <modelType
        else:
            keep_from = max(last_consumed, len(data) - _CARRY)
        carry = data[keep_from:]
        base += keep_from

    if decoded_end is None:
        decoded_end = base + len(carry)

    blocks: list[ModelTypeBlock] = []
    for i, (model_type, start) in enumerate(opens):
        end = opens[i + 1][1] if i + 1 < len(opens) else decoded_end
        blocks.append(ModelTypeBlock(model_type=model_type, start=start, end=end))
    return DecodedIndex(blocks=tuple(blocks), decoded_end=decoded_end, extra_info_start=extra_start)
