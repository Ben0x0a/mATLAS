"""Container & DiscoveredFile for the L0 writer — a folder and an archive are one thing.

Defines:    ``DiscoveredFile`` (one leaf file inside its container chain), the
            ``Container`` protocol, ``FilesystemContainer`` (a folder + all subfolders
            as one container) and ``ZipContainer`` (a zip, from an on-disk path or from
            in-memory bytes for a nested archive). Each container hands out leaf files
            (``files``), opens them (``open``), reveals an on-disk path when one exists
            (``ondisk_path``), fingerprints the main-file bytes and its WAL/SHM/journal
            siblings (``content_sha256`` / ``sibling_sha256``), and materialises a
            leaf onto disk for a reader (``materialise``).
Used by:    ``model_atlas.ingest.discover`` (traversal) and
            ``model_atlas.ingest.writer`` (fingerprints + reader materialisation).
Depends on: ``model_atlas.integrity`` (``sha256_file``), ``model_atlas.sqlite.integrity``
            (``fingerprint_zip`` — the strategic central-directory hash, REUSED, no new
            hashing here) and the standard library only.

Ported from ``sources/container.py`` — the container model (folder and zip
BOTH a ``Container`` addressing files by a logical path) is kept verbatim (a source is
the *opened unit*: folder->file, zip->inner file). The staging/finalize
integrity machinery of the old module is deliberately NOT ported: the readers do
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
# WAL/SHM/journal sidecars pair to these suffixes in this order (the db is
# fingerprinted on its own; each sibling is fingerprinted separately).
SIBLING_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")
SIBLING_KEYS: tuple[str, ...] = ("wal", "shm", "journal")


class ZipBombError(Exception):
    """A zip entry decompresses far past a safe size/ratio — refused to avoid a DoS
    (disk/memory exhaustion) from a malicious or malformed archive."""


@dataclass(frozen=True)
class DiscoveredFile:
    """One leaf file found by discovery, inside the innermost of its container chain.

    A CANDIDATE, held in memory and never persisted: discovery produces one per leaf it
    finds (252 183 on the reference acquisition), the matcher and format detector read it,
    and only the few a preset claims are opened and become ``vocabularies.SourceFile`` rows.
    Named ``DiscoveredFile`` for exactly that reason — the two were both ``SourceFile`` for
    an afternoon and the collision was silent (decision 52).
    """

    containers: tuple["Container", ...]      # outermost .. innermost
    logical_path: PurePosixPath              # path inside the INNERMOST container, no leading slash
    # For a zip leaf: the exact ``infolist()`` index this file was enumerated from, so two
    # members sharing a normalised path each resolve to their OWN entry rather than both
    # collapsing to the first name-match (a silent drop of the second's bytes). None for a
    # filesystem file or a DiscoveredFile built by path alone (e.g. a sibling lookup).
    entry_index: int | None = None

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
        """The chain-inclusive ``inner_path`` elements, or ``[]`` for a loose file.

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

    def files(self) -> Iterable[DiscoveredFile]: ...
    def open(self, file: DiscoveredFile) -> BinaryIO: ...
    def ondisk_path(self, file: DiscoveredFile) -> "Path | None": ...
    def content_sha256(self, file: DiscoveredFile) -> str: ...
    def sibling_sha256(self, file: DiscoveredFile) -> dict[str, str]: ...
    def materialise(self, file: DiscoveredFile, dest_dir: Path) -> Path: ...



# --- Filesystem -----------------------------------------------------------

class FilesystemContainer:
    """A folder (and all its subfolders, unbounded) presented as one Container."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root_label = self.root.name

    def _make_file(self, abspath: Path) -> DiscoveredFile:
        rel = abspath.relative_to(self.root)
        return DiscoveredFile(containers=(self,), logical_path=PurePosixPath(*rel.parts))

    def _abspath(self, file: DiscoveredFile) -> Path:
        return self.root.joinpath(*file.logical_path.parts)

    def files(self) -> Iterable[DiscoveredFile]:
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            yield self._make_file(path)

    def open(self, file: DiscoveredFile) -> BinaryIO:
        return self._abspath(file).open("rb")

    def ondisk_path(self, file: DiscoveredFile) -> Path | None:
        return self._abspath(file)

    def content_sha256(self, file: DiscoveredFile) -> str:
        return sha256_file(self._abspath(file))

    def sibling_sha256(self, file: DiscoveredFile) -> dict[str, str]:
        original = self._abspath(file)
        found: dict[str, str] = {}
        for key, suffix in zip(SIBLING_KEYS, SIBLING_SUFFIXES):
            sibling = original.with_name(file.name + suffix)
            if sibling.exists():
                found[key] = sha256_file(sibling)
        return found

    def materialise(self, file: DiscoveredFile, dest_dir: Path) -> Path:
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
        # ONE handle for the archive's whole life, and ONE name->entry index built from it.
        self._zip: zipfile.ZipFile | None = None
        self._by_name: dict[str, zipfile.ZipInfo] | None = None

    def _zipfile(self) -> zipfile.ZipFile:
        """The archive's single open handle, created on first use and then reused.

        WHY cached rather than opened per call: constructing a ``ZipFile`` PARSES THE WHOLE
        CENTRAL DIRECTORY. Opening one per file therefore costs O(entries) per file and
        O(entries^2) per run — measured on a real 25 GB acquisition (428 483 entries), a
        512-byte head read of 200 files took 487 s that way against 0.02 s through one
        shared handle, which projects to ~170 hours versus ~30 seconds for the archive's
        251 023 files. The archive is opened read-only and never mutated, so one handle is
        safe to share for the sequential walk the pipeline performs.
        """
        if self._zip is None:
            self._zip = (zipfile.ZipFile(self.path, "r") if self.path is not None
                         else zipfile.ZipFile(io.BytesIO(self._data), "r"))
        return self._zip

    def _name_index(self) -> dict[str, zipfile.ZipInfo]:
        """Normalised entry name -> its ``ZipInfo``, built once.

        WHY: ``_find_info``'s linear scan is O(entries) per lookup, and sibling resolution
        performs three per file — on a 428 483-entry archive that is the same quadratic
        trap as reopening the handle. First name wins, matching the previous scan order.
        """
        if self._by_name is None:
            index: dict[str, zipfile.ZipInfo] = {}
            for info in self._zipfile().infolist():
                index.setdefault(info.filename.replace("\\", "/").lstrip("/"), info)
            self._by_name = index
        return self._by_name

    def close(self) -> None:
        """Release the shared handle. Safe to call more than once."""
        if self._zip is not None:
            self._zip.close()
            self._zip = None
            self._by_name = None

    def files(self) -> Iterable[DiscoveredFile]:
        # enumerate the WHOLE infolist so entry_index is the true infolist position (dir
        # entries are skipped but still consume an index), letting _resolve_info map each
        # leaf back to its exact entry — see the duplicate-path handling there.
        for index, info in enumerate(self._zipfile().infolist()):
            if info.is_dir():
                continue
            norm = info.filename.replace("\\", "/").lstrip("/")
            yield DiscoveredFile(
                containers=(self,), logical_path=PurePosixPath(norm), entry_index=index)

    def _find_info(self, zf: zipfile.ZipFile, logical_path: PurePosixPath) -> zipfile.ZipInfo:
        info = self._name_index().get(str(logical_path))
        if info is None:
            raise FileNotFoundError(f"{str(logical_path)!r} not found in {self.root_label}")
        return info

    def _resolve_info(self, zf: zipfile.ZipFile, file: DiscoveredFile) -> zipfile.ZipInfo:
        """The SPECIFIC entry a leaf ``file`` denotes.

        HOW: when the file carries the ``entry_index`` that ``files()`` stamped, return exactly
        that ``infolist()`` entry; otherwise fall back to first-match by normalised name. WHY:
        two members with the same normalised path would otherwise BOTH resolve to the first
        name-match, so the second's bytes are silently dropped — resolving by the enumerated
        index makes leaf reads 1:1 with the entries the walk yielded. A DiscoveredFile built by
        path alone (a sibling lookup) has no index and keeps the name-match path."""
        if file.entry_index is not None:
            return zf.infolist()[file.entry_index]
        return self._find_info(zf, file.logical_path)

    def open(self, file: DiscoveredFile) -> BinaryIO:
        """A stream over one entry, read through the archive's single shared handle.

        The stream does NOT close the archive: the container owns that handle for its
        whole life (see ``_zipfile``) and releases it in ``close``. The previous
        close-the-archive-with-the-stream wrapper existed because each ``open`` created its
        own handle and would otherwise leak it; with one shared handle there is nothing to
        leak, and closing it here would force the next read to re-parse the central
        directory — the very cost this design removes.
        """
        return self._zipfile().open(self._resolve_info(self._zipfile(), file), "r")

    def ondisk_path(self, file: DiscoveredFile) -> Path | None:
        return None  # a file inside an archive has no independent on-disk path

    def _hash_entry(self, zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path | None) -> str:
        """SHA-256 an entry's decompressed bytes, optionally writing them to ``dest``.

        HOW: stream the entry in 1 MiB chunks, updating the hash and (when materialising)
        the output file, tripping ``ZipBombError`` once the entry is large in absolute
        terms AND its expansion ratio is implausible (or the compressed size is unknown).
        WHY one method for both fingerprint and extraction: the decompressed bytes ARE the
        main-file fingerprint of a zipped source, so hashing and extracting share one
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

    def content_sha256(self, file: DiscoveredFile) -> str:
        zf = self._zipfile()
        return self._hash_entry(zf, self._resolve_info(zf, file), None)

    def sibling_sha256(self, file: DiscoveredFile) -> dict[str, str]:
        found: dict[str, str] = {}
        zf = self._zipfile()
        for key, suffix in zip(SIBLING_KEYS, SIBLING_SUFFIXES):
            sib_path = file.logical_path.with_name(file.name + suffix)
            try:
                info = self._find_info(zf, sib_path)
            except FileNotFoundError:
                continue
            found[key] = self._hash_entry(zf, info, None)
        return found

    def materialise(self, file: DiscoveredFile, dest_dir: Path) -> Path:
        """Extract the entry (and any WAL/SHM/journal siblings) into ``dest_dir`` so a
        reader has a real on-disk file. Siblings are extracted beside the db under their
        conventional names so the SQLite reader finds them by the standard naming rule."""
        dest = dest_dir / file.name
        zf = self._zipfile()
        self._hash_entry(zf, self._resolve_info(zf, file), dest)
        for _key, suffix in zip(SIBLING_KEYS, SIBLING_SUFFIXES):
            sib_path = file.logical_path.with_name(file.name + suffix)
            try:
                info = self._find_info(zf, sib_path)
            except FileNotFoundError:
                continue
            self._hash_entry(zf, info, dest_dir / (file.name + suffix))
        return dest
