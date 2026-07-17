"""Container & SourceFile for the L0 writer — a folder and an archive are one thing.

Defines:    ``SourceFile`` (one leaf file inside its container chain), the
            ``Container`` protocol, ``FilesystemContainer`` (a folder + all subfolders
            as one container) and ``ZipContainer`` (a zip, from an on-disk path or from
            in-memory bytes for a nested archive). Each container hands out leaf files
            (``files``), opens them (``open``), reveals an on-disk path when one exists
            (``ondisk_path``), fingerprints the main-file bytes and its WAL/SHM/journal
            siblings (``content_sha256`` / ``sibling_sha256`` — R17), and materialises a
            leaf onto disk for a reader (``materialise``).
Used by:    ``model_atlas.ingest.discover`` (traversal) and
            ``model_atlas.ingest.writer`` (fingerprints + reader materialisation).
Depends on: ``model_atlas.integrity`` (``sha256_file``), ``model_atlas.sqlite.integrity``
            (``fingerprint_zip`` — the strategic central-directory hash, REUSED, no new
            hashing here) and the standard library only.

Ported from ``sources/container.py`` — the container model (folder and zip
BOTH a ``Container`` addressing files by a logical path) is kept verbatim, per decision
29 (a source is the *opened unit*: folder->file, zip->inner file). The staging/finalize
integrity machinery of the old module is deliberately NOT ported: the T20 readers do
their own throwaway-copy staging, so the L0 writer needs only enumeration, opening,
fingerprinting and a plain materialise-onto-disk. The zip-bomb decompression bound is
kept (a nested-archive or a member cannot exhaust the disk).
"""
from __future__ import annotations

import hashlib
import io
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable, Protocol, runtime_checkable

from model_atlas.integrity import sha256_file

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
# WAL/SHM/journal sidecars pair to these suffixes in this order (R17: the db is
# fingerprinted on its own; each sibling is fingerprinted separately).
SIBLING_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")
SIBLING_KEYS: tuple[str, ...] = ("wal", "shm", "journal")


class ZipBombError(Exception):
    """A zip entry decompresses far past a safe size/ratio — refused to avoid a DoS
    (disk/memory exhaustion) from a malicious or malformed archive."""


@dataclass(frozen=True)
class SourceFile:
    """One leaf file inside the innermost of its container chain."""

    containers: tuple["Container", ...]      # outermost .. innermost
    logical_path: PurePosixPath              # path inside the INNERMOST container, no leading slash

    @property
    def name(self) -> str:
        return self.logical_path.name

    @property
    def container(self) -> "Container":
        return self.containers[-1]

    @property
    def zip_labels(self) -> list[str]:
        """The archive names traversed, outermost->innermost, EXCLUDING the filesystem
        root — ``["a.zip", "b.zip"]`` for a nested member, ``[]`` for a loose file."""
        return [c.root_label for c in self.containers if isinstance(c, ZipContainer)]

    @property
    def inner_chain(self) -> list[str]:
        """The chain-inclusive ``inner_path`` elements (R10), or ``[]`` for a loose file.

        HOW: the archive labels (outermost->innermost) then this leaf's logical path, so
        ``core.determinism.container_inner_path`` joins them into ``a.zip!b.zip!x.db``.
        WHY empty for a loose file: a file that sits directly on the filesystem has NO
        container path, so its ``inner_path`` column is NULL — only archived files carry
        a chain (schema: ``inner_path`` = chain-inclusive container path).
        """
        labels = self.zip_labels
        if not labels:
            return []
        return labels + [str(self.logical_path)]


@runtime_checkable
class Container(Protocol):
    root_label: str

    def files(self) -> Iterable[SourceFile]: ...
    def open(self, file: SourceFile) -> BinaryIO: ...
    def ondisk_path(self, file: SourceFile) -> "Path | None": ...
    def content_sha256(self, file: SourceFile) -> str: ...
    def sibling_sha256(self, file: SourceFile) -> dict[str, str]: ...
    def materialise(self, file: SourceFile, dest_dir: Path) -> Path: ...


class _ClosingZipStream:
    """An open zip entry that also closes its parent ``ZipFile`` when closed.

    ``ZipFile.open`` returns a stream over an entry, but closing that stream does NOT
    close the ``ZipFile`` itself, so a bare ``zf.open(info)`` leaks the archive handle
    until GC. Discovery opens entries once per candidate file, so on a large acquisition
    that leak can exhaust file descriptors. This wrapper delegates every stream operation
    to the entry and closes BOTH on exit."""

    def __init__(self, inner: BinaryIO, zf: zipfile.ZipFile) -> None:
        self._inner = inner
        self._zf = zf

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def __enter__(self) -> "_ClosingZipStream":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def close(self) -> None:
        try:
            self._inner.close()
        finally:
            self._zf.close()


# --- Filesystem -----------------------------------------------------------

class FilesystemContainer:
    """A folder (and all its subfolders, unbounded) presented as one Container."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root_label = self.root.name

    def _make_file(self, abspath: Path) -> SourceFile:
        rel = abspath.relative_to(self.root)
        return SourceFile(containers=(self,), logical_path=PurePosixPath(*rel.parts))

    def _abspath(self, file: SourceFile) -> Path:
        return self.root.joinpath(*file.logical_path.parts)

    def files(self) -> Iterable[SourceFile]:
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            yield self._make_file(path)

    def open(self, file: SourceFile) -> BinaryIO:
        return self._abspath(file).open("rb")

    def ondisk_path(self, file: SourceFile) -> Path | None:
        return self._abspath(file)

    def content_sha256(self, file: SourceFile) -> str:
        return sha256_file(self._abspath(file))

    def sibling_sha256(self, file: SourceFile) -> dict[str, str]:
        original = self._abspath(file)
        found: dict[str, str] = {}
        for key, suffix in zip(SIBLING_KEYS, SIBLING_SUFFIXES):
            sibling = original.with_name(file.name + suffix)
            if sibling.exists():
                found[key] = sha256_file(sibling)
        return found

    def materialise(self, file: SourceFile, dest_dir: Path) -> Path:
        """A loose file is already on disk, so hand back its real path unchanged.

        WHY no copy: reading is read-only and the readers that MUST work on a private
        copy (the SQLite reader's WAL two-pass) copy the file themselves; copying here
        would duplicate multi-GB evidence for no gain."""
        return self._abspath(file)


# --- Zip ------------------------------------------------------------------

class ZipContainer:
    """A zip archive presented as a Container. Built from a path (an on-disk archive) or
    from raw bytes (a nested archive). The archive is never mutated during a run.

    Decompression is bounded against zip bombs: an entry that expands beyond
    ``max_decompression_ratio`` times its compressed size, once past
    ``min_ratio_check_bytes``, is refused with a ``ZipBombError`` rather than filling the
    disk. The floor keeps the check off small entries (whose ratio is meaningless); a
    large but modestly-compressed evidence file (e.g. a multi-GB SQLite DB) passes."""

    max_decompression_ratio: int = 200
    min_ratio_check_bytes: int = 64 * 1024 * 1024

    def __init__(
        self, *, path: Path | None = None, data: bytes | None = None, label: str | None = None,
    ) -> None:
        if path is None and data is None:
            raise ValueError("ZipContainer needs a path or bytes")
        self.path = Path(path) if path is not None else None
        self._data = data
        self.root_label = label or (self.path.name if self.path else "archive")

    def _zipfile(self) -> zipfile.ZipFile:
        if self.path is not None:
            return zipfile.ZipFile(self.path, "r")
        return zipfile.ZipFile(io.BytesIO(self._data), "r")

    def files(self) -> Iterable[SourceFile]:
        with self._zipfile() as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                norm = info.filename.replace("\\", "/").lstrip("/")
                yield SourceFile(containers=(self,), logical_path=PurePosixPath(norm))

    def _find_info(self, zf: zipfile.ZipFile, logical_path: PurePosixPath) -> zipfile.ZipInfo:
        target = str(logical_path)
        for info in zf.infolist():
            if info.filename.replace("\\", "/").lstrip("/") == target:
                return info
        raise FileNotFoundError(f"{target!r} not found in {self.root_label}")

    def open(self, file: SourceFile) -> BinaryIO:
        zf = self._zipfile()
        try:
            info = self._find_info(zf, file.logical_path)
            return _ClosingZipStream(zf.open(info), zf)
        except BaseException:
            zf.close()
            raise

    def ondisk_path(self, file: SourceFile) -> Path | None:
        return None  # a file inside an archive has no independent on-disk path

    def _hash_entry(self, zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path | None) -> str:
        """SHA-256 an entry's decompressed bytes, optionally writing them to ``dest``.

        HOW: stream the entry in 1 MiB chunks, updating the hash and (when materialising)
        the output file, tripping ``ZipBombError`` once the entry is large in absolute
        terms AND its expansion ratio is implausible (or the compressed size is unknown).
        WHY one method for both fingerprint and extraction: the decompressed bytes ARE the
        R17 main-file fingerprint of a zipped source, so hashing and extracting share one
        bounded read path and can never disagree on what was read."""
        h = hashlib.sha256()
        written = 0
        floor = self.min_ratio_check_bytes
        ratio = self.max_decompression_ratio
        out = open(dest, "wb") if dest is not None else None
        try:
            with zf.open(info) as src:
                for chunk in iter(lambda: src.read(_CHUNK), b""):
                    written += len(chunk)
                    if written > floor and (
                        info.compress_size == 0 or written > info.compress_size * ratio
                    ):
                        raise ZipBombError(
                            f"entry {info.filename!r} decompresses past the safe limit "
                            f"({written} bytes from {info.compress_size} compressed, "
                            f"ratio cap {ratio}); refusing to extract"
                        )
                    h.update(chunk)
                    if out is not None:
                        out.write(chunk)
        finally:
            if out is not None:
                out.close()
        return h.hexdigest()

    def content_sha256(self, file: SourceFile) -> str:
        with self._zipfile() as zf:
            return self._hash_entry(zf, self._find_info(zf, file.logical_path), None)

    def sibling_sha256(self, file: SourceFile) -> dict[str, str]:
        found: dict[str, str] = {}
        with self._zipfile() as zf:
            for key, suffix in zip(SIBLING_KEYS, SIBLING_SUFFIXES):
                sib_path = file.logical_path.with_name(file.name + suffix)
                try:
                    info = self._find_info(zf, sib_path)
                except FileNotFoundError:
                    continue
                found[key] = self._hash_entry(zf, info, None)
        return found

    def materialise(self, file: SourceFile, dest_dir: Path) -> Path:
        """Extract the entry (and any WAL/SHM/journal siblings) into ``dest_dir`` so a
        reader has a real on-disk file. Siblings are extracted beside the db under their
        conventional names so the SQLite reader finds them by the standard naming rule."""
        dest = dest_dir / file.name
        with self._zipfile() as zf:
            self._hash_entry(zf, self._find_info(zf, file.logical_path), dest)
            for _key, suffix in zip(SIBLING_KEYS, SIBLING_SUFFIXES):
                sib_path = file.logical_path.with_name(file.name + suffix)
                try:
                    info = self._find_info(zf, sib_path)
                except FileNotFoundError:
                    continue
                self._hash_entry(zf, info, dest_dir / (file.name + suffix))
        return dest
