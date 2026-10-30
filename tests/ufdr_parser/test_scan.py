"""Fuzz the CDATA-aware byte scanner against a reference walk, across chunk boundaries.

Defines:    a reference (token-by-token, whole-buffer) implementation and randomized reports
            full of decoy markers inside CDATA, read in tiny chunks so every token is split at
            every possible boundary.
Used by:    pytest.
Depends on: ufdr_parser.scan.
"""
from __future__ import annotations

import io
import random
import re

import pytest

from ufdr_parser import scan

_TOKENS = re.compile(rb"<!\[CDATA\[|\]\]>|<decodedData|</decodedData>|<modelType|<extraInfos")


def _reference(data: bytes) -> tuple[int | None, list[tuple[str, int]], int | None, int | None]:
    """(decodedData start, [(type, start)], decoded end, extraInfos start), walking every token."""
    inside = False
    decoded = None
    opens: list[tuple[str, int]] = []
    decoded_end = extra = None
    for match in _TOKENS.finditer(data):
        token, pos = match.group(), match.start()
        if inside:
            inside = token != b"]]>"
            continue
        if token == b"<![CDATA[":
            inside = True
        elif token == b"<decodedData" and decoded is None:
            decoded = pos
        elif decoded is None:
            continue
        elif token == b"<modelType":
            close = data.find(b">", pos)
            attr = re.compile(rb'\btype="([^"]*)"').search(data, pos, close + 1)
            opens.append((attr.group(1).decode() if attr else "", pos))
        elif token == b"</decodedData>" and decoded_end is None:
            decoded_end = pos
        elif token == b"<extraInfos" and extra is None:
            extra = pos
    return decoded, opens, decoded_end, extra


_PIECES = [
    b"<![CDATA[", b"]]>", b"<decodedData", b"<modelType type=\"T\">", b"</decodedData>",
    b"<extraInfos>", b"text", b"<field>", b"]", b"<", b"!", b"[CDATA[", b"\n",
]


def _random_report(rng: random.Random) -> bytes:
    head = b"".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 40)))
    body = b"".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 60)))
    # Close any CDATA left open in the header so a real anchor exists, as in a valid report.
    return head + b"]]>" + b"<decodedData>" + body + b"]]></decodedData><extraInfos>"


@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7, 13, 64, 4096])
def test_scanner_matches_reference(monkeypatch: pytest.MonkeyPatch, chunk: int) -> None:
    monkeypatch.setattr(scan, "_CHUNK", chunk)
    monkeypatch.setattr(scan, "_CARRY", 32)
    rng = random.Random(chunk)
    for _ in range(400):
        data = _random_report(rng)
        decoded, opens, decoded_end, extra = _reference(data)
        tail, offset = scan.find_decoded_data(io.BytesIO(data))
        assert offset == decoded and data[offset:].startswith(tail[:12])
        index = scan.index_model_types(io.BytesIO(data))
        assert [(b.model_type, b.start) for b in index.blocks] == opens
        assert index.decoded_end == decoded_end and index.extra_info_start == extra
