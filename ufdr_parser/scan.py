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

    carry = b""
    base = 0  # absolute offset of carry[0]
    in_cdata = False
    in_decoded = False  # before decodedData we only hunt the section start (cheap)

    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        data = carry + chunk
        # Phase 1: cheaply skip taggedFiles. It is CDATA-dense but irrelevant, so we just
        # plain-find <decodedData and avoid paying the regex/CDATA cost over ~73% of the file.
        if not in_decoded:
            hit = data.find(b"<decodedData")
            if hit == -1:
                carry = data[-_CARRY:]
                base += len(data) - len(carry)
                continue
            in_decoded = True
            # Re-enter the regex scan from the decodedData marker within this chunk.
            data = data[hit:]
            base += hit
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
