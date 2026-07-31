"""Walk an input (folder or archive) into a stream of archive + leaf-file events.

Defines:    the ``ArchiveFound`` / ``FileFound`` walk events and ``walk`` — a generator
            that traverses a folder or archive via the Container model, emitting one
            ``ArchiveFound`` per container encountered (with its central-directory hash
            and whether the walk skipped it) and one ``FileFound`` per leaf file.
Used by:    ``model_atlas.ingest.writer`` (the L0 writer consumes the events and persists
            ``archive`` / ``run_archive`` / ``source`` / ``raw_record`` rows).
Depends on: ``model_atlas.ingest.containers`` (the Container model), ``model_atlas.ingest.
            format_detect`` (recognising a nested archive), ``model_atlas.core.determinism``
            (``canonical_path`` for the on-disk archive identity), ``model_atlas.sqlite.
            integrity`` (``fingerprint_zip`` — the central-directory hash, REUSED) and the
            standard library.

Ported from ``sources/discover.py``. Two changes vs the original, both
justified in the writer's header:
  * the walk now EMITS an ``ArchiveFound`` event per container (the old discover returned
    only leaf ``SourceFile``s), so the writer can record every archive encounter and an
    opt-in preset-scoped skip — never traceless;
  * a ``should_skip(cd_sha256)`` predicate lets the writer stop the walk descending into
    an archive a previous run already parsed with the same preset set (a skip still emits
    its ``ArchiveFound``, with ``skipped=True``).

``max_container_depth`` counts ARCHIVE NESTING only, exactly as the original: a folder
input is depth 0 (its archives open at the default 1); a direct archive input is depth 1
(opening it already spends one level, so nested archives stay off by default). Folder-tree
depth is unbounded (a folder + all subfolders is ONE container).
"""
from __future__ import annotations

import logging
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

from model_atlas.core.determinism import canonical_path
from model_atlas.ingest.containers import (
    SIBLING_SUFFIXES,
    Container,
    FilesystemContainer,
    SourceFile,
    ZipContainer,
)
from model_atlas.ingest.format_detect import detect_path_format, detect_source_format
from model_atlas.sqlite.integrity import fingerprint_zip

log = logging.getLogger(__name__)

# A nested archive must be buffered in memory to be listed; cap that buffer so a
# zip-bomb-as-nested-archive cannot exhaust memory. On-disk archives are read lazily by
# path and are not subject to this. Per-entry decompression is bounded separately in
# ZipContainer (ZipBombError).
_NESTED_ARCHIVE_MAX_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class ArchiveFound:
    """One container (zip) encountered during the walk.

    ``path`` is the container's stable identity: the canonical on-disk path for a real
    archive, or ``<parent path>!<member>`` for one nested inside another archive.
    ``cd_sha256`` is the strategic central-directory hash (reused from sqlite.integrity),
    the queryable "same archive bytes" key. ``skipped`` records an opt-in, preset-scoped
    skip whose subtree the walk did NOT descend."""

    path: str
    cd_sha256: str
    skipped: bool


@dataclass(frozen=True)
class FileFound:
    """One leaf file to be opened as a source (folder->file, zip->inner file)."""

    file: SourceFile


def _sidecar_base(name: str) -> str | None:
    """The sibling db name a ``-wal`` / ``-shm`` / ``-journal`` file pairs with, or None when
    ``name`` is not a sidecar at all.

    WHY the base name and not a bool: a sidecar is only redundant with the db read when its db
    is actually present; an ORPHANED sidecar (a partial acquisition, or an unrelated
    ``*-journal``) has no sibling db, so it must be emitted as a normal file rather than
    dropped — a forensic walk must leave no input traceless."""
    for suffix in SIBLING_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return None


def _cd_sha256_from_path(path: Path) -> str:
    """The central-directory sha256 of an on-disk zip (fingerprint_zip, no new hashing)."""
    return fingerprint_zip(path, {}).cd_sha256


def _cd_sha256_from_bytes(data: bytes) -> str:
    """The central-directory sha256 of an in-memory nested archive.

    HOW: ``fingerprint_zip`` needs a path, so the buffered bytes are written to a
    throwaway temp file and fingerprinted there, then removed.
    WHY reuse it rather than hash inline: every archive must carry the SAME strategic hash,
    on disk or nested, so the "same bytes twice" finding is comparable."""
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        return _cd_sha256_from_path(Path(tmp.name))


def _walk_container(
    container: Container,
    prefix: tuple[Container, ...],
    depth: int,
    max_container_depth: int,
    should_skip: Callable[[str], bool],
) -> Iterator[ArchiveFound | FileFound]:
    # Materialise the listing so an orphaned sidecar can be told from a live one: a sidecar is
    # only skippable when its db sibling is ALSO present in this container.
    files = list(container.files())
    present = {str(candidate.logical_path) for candidate in files}
    for file in files:
        sidecar_base = _sidecar_base(file.name)
        if sidecar_base is not None:
            sibling_db = str(file.logical_path.with_name(sidecar_base))
            if sibling_db in present:
                # A live -wal/-shm/-journal beside its db: the SQLite reader handles it via the
                # db (two-pass merge), so it is not a source of its own — skip it.
                log.debug(f"Skipping SQLite sidecar beside its db during discovery: {file.logical_path}")
                continue
            # else: an ORPHAN sidecar (no sibling db) or an unrelated *-journal — fall through
            # and emit it as a normal file so its bytes are fingerprinted, never dropped.
            log.debug(f"Orphan sidecar with no sibling db, emitting as a file: {file.logical_path}")
        # Re-anchor the file onto the full container chain (outermost..innermost), keeping the
        # entry_index so a zip leaf still resolves to its OWN entry (duplicate-path handling).
        file = SourceFile(
            containers=prefix + (container,),
            logical_path=file.logical_path,
            entry_index=file.entry_index,
        )
        if depth < max_container_depth and detect_source_format(file) == "archive":
            yield from _open_nested(file, container, prefix, depth, max_container_depth, should_skip)
            continue
        yield FileFound(file)


def _archive_identity(container: Container, file: SourceFile, disk: Path | None) -> str:
    """The archive ``path`` identity for a nested container: the on-disk canonical path,
    or ``<parent path>!<member>`` when it lives inside another archive (unique + traceable)."""
    if disk is not None:
        return canonical_path(str(disk.resolve()))
    parent = _container_identity(container)
    return f"{parent}!{file.logical_path}"


def _container_identity(container: Container) -> str:
    """The stable identity string of an already-opened container (for chaining nested paths)."""
    if isinstance(container, ZipContainer) and container.path is not None:
        return canonical_path(str(container.path.resolve()))
    if isinstance(container, ZipContainer):
        return container.root_label
    return canonical_path(str(container.root.resolve()))


def _open_nested(
    file: SourceFile,
    container: Container,
    prefix: tuple[Container, ...],
    depth: int,
    max_container_depth: int,
    should_skip: Callable[[str], bool],
) -> Iterator[ArchiveFound | FileFound]:
    """Open a nested archive, emit its ``ArchiveFound`` and (unless skipped) descend."""
    disk = container.ondisk_path(file)
    try:
        if disk is not None:
            nested = ZipContainer(path=disk, label=file.name)
            cd_sha256 = _cd_sha256_from_path(disk)
        else:
            with container.open(file) as fh:
                data = fh.read(_NESTED_ARCHIVE_MAX_BYTES + 1)
            if len(data) > _NESTED_ARCHIVE_MAX_BYTES:
                log.warning(
                    f"nested archive {file.logical_path} exceeds {_NESTED_ARCHIVE_MAX_BYTES} "
                    f"bytes; treating as an opaque file"
                )
                yield FileFound(file)
                return
            nested = ZipContainer(data=data, label=file.name)
            cd_sha256 = _cd_sha256_from_bytes(data)
    except (OSError, zipfile.BadZipFile):
        # A file that looked like an archive but cannot be opened (truncated / corrupt
        # acquisition). Surface it (not debug) and keep it as an opaque file so the run
        # continues and the corruption is visible, not silent.
        log.warning(f"could not open archive {file.logical_path}; treating as an opaque file")
        yield FileFound(file)
        return

    path = _archive_identity(container, file, disk)
    if should_skip(cd_sha256):
        log.info(f"skipping already-parsed archive {path} (opt-in, same preset set)")
        yield ArchiveFound(path=path, cd_sha256=cd_sha256, skipped=True)
        return
    yield ArchiveFound(path=path, cd_sha256=cd_sha256, skipped=False)
    yield from _walk_container(
        nested, prefix + (container,), depth + 1, max_container_depth, should_skip
    )


def walk(
    input_path: Path,
    *,
    max_container_depth: int = 1,
    should_skip: Callable[[str], bool] = lambda cd_sha256: False,
) -> Iterator[ArchiveFound | FileFound]:
    """Traverse ``input_path``, yielding ``ArchiveFound`` / ``FileFound`` events in order.

    A folder input walks at depth 0 (its archives open at the default depth 1); a direct
    archive input is depth 1 (it is emitted as an ``ArchiveFound`` and, unless skipped,
    walked); a single ordinary file yields exactly one ``FileFound``.
    """
    input_path = Path(input_path)
    if input_path.is_dir():
        top: Container = FilesystemContainer(input_path)
        yield from _walk_container(top, (), 0, max_container_depth, should_skip)
        return
    if not input_path.is_file():
        raise FileNotFoundError(input_path)

    fmt = detect_path_format(input_path)
    if fmt == "archive":
        top = ZipContainer(path=input_path)
        path = canonical_path(str(input_path.resolve()))
        try:
            cd_sha256 = _cd_sha256_from_path(input_path)
        except (OSError, zipfile.BadZipFile) as exc:
            # The input has archive magic but is not a readable zip (truncated / corrupt);
            # fail with a clear, actionable error rather than a raw traceback.
            raise ValueError(f"input archive is not a readable zip: {input_path}") from exc
        if should_skip(cd_sha256):
            yield ArchiveFound(path=path, cd_sha256=cd_sha256, skipped=True)
            return
        yield ArchiveFound(path=path, cd_sha256=cd_sha256, skipped=False)
        try:
            yield from _walk_container(top, (), 1, max_container_depth, should_skip)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"input archive is not a readable zip: {input_path}") from exc
        return

    # A single ordinary file (csv / sqlite / xlsx / unknown): present it through a
    # FilesystemContainer over its parent yielding just this one file.
    container = FilesystemContainer(input_path.parent)
    file = SourceFile(containers=(container,), logical_path=PurePosixPath(input_path.name))
    yield FileFound(file)
