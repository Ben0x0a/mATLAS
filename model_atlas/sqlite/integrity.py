"""SQLite source-integrity helpers.

Defines full-file SHA-256 helpers (``sha256_file``, ``snapshot``,
``verify_unchanged``) and a strategic ZIP fingerprint
(``fingerprint_zip``/``verify_zip_fingerprint``) plus report dataclasses. The
SQLite source adapter uses these reports in extraction metadata and the
source-agnostic traceability sidecar.

Two integrity strategies:
  * FULL — whole-file SHA-256 of each source (zip/sqlite/wal/shm/journal),
    hashed before copying and again after the run. Reads the whole file twice;
    used for direct SQLite sources and as the opt-in mode for zips.
  * STRATEGIC (zips) — proves the tool did not modify the archive by hashing
    only the bytes that matter: total size, the central-directory region
    (cd_offset..EOF) and, for each extracted entry, its raw byte span plus the
    SHA-256 of the decompressed content. Reads only the touched regions, so a
    200 GB archive costs a few entry reads instead of two full passes. Every
    component is recorded in the traceability sidecar and is reproducible by
    hand with stat/dd/unzip/sha256sum (see ``strategic_method_doc``).
"""
from __future__ import annotations

import hashlib
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

_CHUNK = 1024 * 1024  # 1 MiB

_LOCAL_HEADER_FIXED = 30          # bytes of the fixed local-file-header prefix
_LOCAL_HEADER_SIG = 0x04034B50    # "PK\x03\x04"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(paths: Dict[str, Path]) -> Dict[str, str]:
    """Hash a set of named files. Missing paths are skipped silently."""
    return {name: sha256_file(p) for name, p in paths.items() if p is not None and p.exists()}


@dataclass
class IntegrityReport:
    ok: bool
    per_file: Dict[str, Dict[str, object]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "per_file": self.per_file}


def verify_unchanged(before: Dict[str, str], paths: Dict[str, Path]) -> IntegrityReport:
    per_file: Dict[str, Dict[str, object]] = {}
    all_ok = True
    for name, before_hash in before.items():
        path = paths.get(name)
        if path is None or not path.exists():
            per_file[name] = {"before": before_hash, "after": None, "match": False, "note": "missing after"}
            all_ok = False
            continue
        after_hash = sha256_file(path)
        match = after_hash == before_hash
        per_file[name] = {"before": before_hash, "after": after_hash, "match": match}
        if not match:
            all_ok = False
    return IntegrityReport(ok=all_ok, per_file=per_file)


def format_report(report: IntegrityReport) -> str:
    lines = []
    header = "Integrity check: OK" if report.ok else "Integrity check: FAILED"
    lines.append(header)
    for name, info in report.per_file.items():
        mark = "[OK]" if info.get("match") else "[MISMATCH]"
        lines.append(f"  {mark} {name}")
        if not info.get("match"):
            lines.append(f"      before: {info.get('before')}")
            lines.append(f"      after:  {info.get('after')}")
    return "\n".join(lines)


# --- strategic zip fingerprint --------------------------------------------

def _sha256_range(fh, start: int, length: int) -> str:
    """SHA-256 over ``[start, start+length)`` of an open binary file, chunked.

    A seek past EOF or a short read (e.g. the file was truncated) simply hashes
    fewer bytes, which yields a mismatch on verification — exactly what we want.
    """
    h = hashlib.sha256()
    fh.seek(start)
    remaining = max(0, length)
    while remaining > 0:
        chunk = fh.read(min(_CHUNK, remaining))
        if not chunk:
            break
        h.update(chunk)
        remaining -= len(chunk)
    return h.hexdigest()


def _entry_data_offset(fh, header_offset: int) -> int:
    """Resolve where an entry's compressed data starts by reading its LOCAL
    file header. The local extra-field length can differ from the central
    directory's copy, so this is read from the file rather than derived from
    ``ZipInfo`` — keeping the hashed span byte-exact and hand-reproducible."""
    fh.seek(header_offset)
    fixed = fh.read(_LOCAL_HEADER_FIXED)
    if len(fixed) < _LOCAL_HEADER_FIXED:
        raise ValueError(f"truncated local file header at offset {header_offset}")
    (sig,) = struct.unpack("<I", fixed[0:4])
    if sig != _LOCAL_HEADER_SIG:
        raise ValueError(f"bad local file header signature at offset {header_offset}")
    name_len, extra_len = struct.unpack("<HH", fixed[26:30])
    return header_offset + _LOCAL_HEADER_FIXED + name_len + extra_len


@dataclass
class ZipEntryFingerprint:
    """Integrity record for one extracted zip entry (db / wal / shm / journal)."""
    arcname: str
    local_header_offset: int
    data_offset: int            # start of compressed data (after local header)
    compressed_size: int
    compressed_sha256: str      # span [local_header_offset, data_offset+compressed_size)
    uncompressed_size: int
    content_sha256: str         # decompressed bytes — the evidence itself

    def to_dict(self) -> dict:
        return {
            "arcname": self.arcname,
            "local_header_offset": self.local_header_offset,
            "data_offset": self.data_offset,
            "compressed_size": self.compressed_size,
            "compressed_sha256": self.compressed_sha256,
            "uncompressed_size": self.uncompressed_size,
            "content_sha256": self.content_sha256,
        }


@dataclass
class ZipFingerprint:
    """Strategic integrity fingerprint of a zip archive — see module docstring."""
    algorithm: str
    file_size: int
    cd_offset: int
    cd_size: int
    cd_sha256: str
    entries: List[ZipEntryFingerprint] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "algorithm": self.algorithm,
            "file_size": self.file_size,
            "central_directory": {
                "offset": self.cd_offset,
                "size": self.cd_size,
                "sha256": self.cd_sha256,
            },
            "entries": [e.to_dict() for e in self.entries],
        }

    def resume_key(self) -> str:
        """Cheap change-detection key: size + central-directory hash.

        The central directory embeds every entry's CRC-32 and sizes, so this
        changes whenever any entry is added, removed, resized or its content
        altered without re-reading the whole archive.
        """
        return f"strategic:{self.file_size}:{self.cd_sha256}"


def fingerprint_zip(path: Path, entries: Dict[str, "zipfile.ZipInfo"]) -> ZipFingerprint:
    """Build a strategic fingerprint of ``path``, hashing only the touched
    regions: the central-directory region and the spans/content of ``entries``
    (the db + sidecars we extract). Never reads the whole archive."""
    path = Path(path)
    file_size = path.stat().st_size
    with zipfile.ZipFile(path, "r") as zf:
        cd_offset = zf.start_dir  # offset of the first central-directory record
        entry_fps: List[ZipEntryFingerprint] = []
        with open(path, "rb") as fh:
            cd_sha = _sha256_range(fh, cd_offset, file_size - cd_offset)
            for info in entries.values():
                ho = info.header_offset
                data_off = _entry_data_offset(fh, ho)
                span_len = (data_off - ho) + info.compress_size
                comp_sha = _sha256_range(fh, ho, span_len)
                content = hashlib.sha256()
                with zf.open(info, "r") as src:
                    for chunk in iter(lambda: src.read(_CHUNK), b""):
                        content.update(chunk)
                entry_fps.append(
                    ZipEntryFingerprint(
                        arcname=info.filename,
                        local_header_offset=ho,
                        data_offset=data_off,
                        compressed_size=info.compress_size,
                        compressed_sha256=comp_sha,
                        uncompressed_size=info.file_size,
                        content_sha256=content.hexdigest(),
                    )
                )
    return ZipFingerprint(
        algorithm="sha256",
        file_size=file_size,
        cd_offset=cd_offset,
        cd_size=file_size - cd_offset,
        cd_sha256=cd_sha,
        entries=entry_fps,
    )


def verify_zip_fingerprint(before: ZipFingerprint, path: Path) -> IntegrityReport:
    """Recompute only the structural parts (file size, central-directory region,
    each entry's compressed span) and compare to ``before``. The decompressed
    content is a pure function of the compressed bytes, so a matching compressed
    span implies matching content — we deliberately skip re-decompression to
    keep verification cheap on huge archives."""
    path = Path(path)
    per_file: Dict[str, Dict[str, object]] = {}
    all_ok = True

    file_size = path.stat().st_size
    size_match = file_size == before.file_size
    per_file["file_size"] = {"before": before.file_size, "after": file_size, "match": size_match}
    all_ok = all_ok and size_match

    with open(path, "rb") as fh:
        cd_after = _sha256_range(fh, before.cd_offset, file_size - before.cd_offset)
        cd_match = cd_after == before.cd_sha256
        per_file["central_directory"] = {"before": before.cd_sha256, "after": cd_after, "match": cd_match}
        all_ok = all_ok and cd_match

        for e in before.entries:
            span_len = (e.data_offset - e.local_header_offset) + e.compressed_size
            after = _sha256_range(fh, e.local_header_offset, span_len)
            match = after == e.compressed_sha256
            per_file[e.arcname] = {"before": e.compressed_sha256, "after": after, "match": match}
            all_ok = all_ok and match

    return IntegrityReport(ok=all_ok, per_file=per_file)


def zip_resume_key(path: Path) -> str:
    """Cheap change-detection key for a ZIP archive.

    Reads only the central-directory region, not the whole archive. The central
    directory embeds every entry's CRC-32 and sizes, so this key changes
    whenever any entry is added, removed, resized or altered.
    """
    path = Path(path)
    file_size = path.stat().st_size
    with zipfile.ZipFile(path, "r") as zf:
        cd_offset = zf.start_dir
    with open(path, "rb") as fh:
        cd_sha = _sha256_range(fh, cd_offset, file_size - cd_offset)
    return f"strategic:{file_size}:{cd_sha}"


def strategic_method_doc() -> str:
    """Human-readable recipe for reproducing the strategic fingerprint by hand,
    embedded in the traceability sidecar so a third party can verify it with
    standard tools."""
    return (
        "Strategic zip integrity (sha256) — reproduce each component by hand:\n"
        "  file_size:                 stat -c %s <zip>\n"
        "  central_directory.sha256:  dd if=<zip> bs=1M iflag=skip_bytes "
        "skip=<central_directory.offset> | sha256sum   (bytes offset..EOF; "
        "includes the central directory and EOCD / zip64-EOCD)\n"
        "  entry.compressed_sha256:   dd if=<zip> iflag=skip_bytes,count_bytes "
        "skip=<entry.local_header_offset> "
        "count=<(entry.data_offset-entry.local_header_offset)+entry.compressed_size> "
        "| sha256sum\n"
        "  entry.content_sha256:      unzip -p <zip> '<entry.arcname>' | sha256sum\n"
        "Scope: detects modification by this tool of the archive structure and of "
        "the entries it extracts. The central directory stores every entry's "
        "CRC-32 and sizes, so hashing it also detects content changes to other "
        "entries. This is NOT a whole-file hash and is not a chain-of-custody "
        "record; use the full-source-hash option when a regulator requires a "
        "single whole-file digest."
    )
