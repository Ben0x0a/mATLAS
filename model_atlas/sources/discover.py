"""Discover SourceFiles from an input file or folder via the Container model.

Defines:    discover(input_path, *, max_container_depth=1) -> tuple[SourceFile, ...].
Used by:    the pipeline (replaces the old folder.discover_elements).
Depends on: container, format_detect.

A folder and an archive are both Containers. ``max_container_depth`` counts ARCHIVE
NESTING only. A **folder input is depth 0** (not itself an archive), an **archive input is
depth 1** (opening it already spends one level). So at the default ``1``:
  - a folder input is walked AND the archives inside it are opened and treated as
    acquisitions (depth 0 -> 1), but archives nested inside those are not (would be 2);
  - a direct archive input is opened (it is depth 1) but archives nested inside it are
    NOT explored (would be 2) — a direct zip is deliberately not depth 0 so nested-archive
    exploration stays off by default.
Folder-tree depth is unbounded throughout (a folder + all subfolders is ONE container).
"""
from __future__ import annotations

import logging
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable

from model_atlas.sources.container import Container, FilesystemContainer, SourceFile, ZipContainer
from model_atlas.sources.format_detect import detect_format

log = logging.getLogger(__name__)

_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_HEAD_BYTES = 16


def _is_sidecar(name: str) -> bool:
    return name.endswith(_SIDECAR_SUFFIXES)


def _peek_head(container: Container, file: SourceFile) -> bytes:
    try:
        with container.open(file) as fh:
            return fh.read(_HEAD_BYTES)
    except (OSError, KeyError, zipfile.BadZipFile):
        log.debug(f"Could not read head of {file.logical_path}", exc_info=True)
        return b""


def _walk(
    container: Container,
    prefix: tuple[Container, ...],
    depth: int,
    max_container_depth: int,
) -> Iterable[SourceFile]:
    for file in container.files():
        if _is_sidecar(file.name):
            log.debug(f"Skipping SQLite sidecar during discovery: {file.logical_path}")
            continue
        # Re-anchor the file onto the full container chain (outermost..innermost).
        file = SourceFile(containers=prefix + (container,), logical_path=file.logical_path)
        if depth < max_container_depth:
            head = _peek_head(container, file)
            if head[:4] == b"PK\x03\x04" and detect_format(head, lambda: _peek_zip_names(container, file), file.logical_path.suffix) == "archive":
                # TODO: zip-bomb max-depth guard — bound total decompressed bytes here.
                try:
                    # A zip on the filesystem keeps its on-disk path (lazy reads, and the
                    # path backs input_file_path); a zip nested in another zip has no path,
                    # so fall back to its decompressed bytes.
                    disk = container.ondisk_path(file)
                    if disk is not None:
                        nested = ZipContainer(path=disk, label=file.name)
                    else:
                        with container.open(file) as fh:
                            data = fh.read()
                        nested = ZipContainer(data=data, label=file.name)
                    yield from _walk(nested, prefix + (container,), depth + 1, max_container_depth)
                    continue
                except (OSError, zipfile.BadZipFile):
                    log.debug(f"Failed to open nested archive {file.logical_path}", exc_info=True)
        yield file


def _peek_zip_names(container: Container, file: SourceFile) -> list[str]:
    try:
        with container.open(file) as fh:
            with zipfile.ZipFile(fh) as zf:
                return zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return []


def discover(input_path: Path, *, max_container_depth: int = 1) -> tuple[SourceFile, ...]:
    input_path = Path(input_path)
    if input_path.is_dir():
        # A folder is depth 0 (not an archive): at the default depth 1 the archives it
        # contains are opened and treated as acquisitions.
        top: Container = FilesystemContainer(input_path)
        files = tuple(_walk(top, (), 0, max_container_depth))
        log.info(f"Discovered {len(files)} file(s) from folder {input_path}")
        return files
    if not input_path.is_file():
        raise FileNotFoundError(input_path)

    with input_path.open("rb") as fh:
        head = fh.read(_HEAD_BYTES)
    fmt = detect_format(head, lambda: _zip_namelist(input_path), input_path.suffix)
    if fmt == "archive":
        top = ZipContainer(path=input_path)
        files = tuple(_walk(top, (), 1, max_container_depth))
        log.info(f"Discovered {len(files)} file(s) from archive {input_path}")
        return files

    # A single ordinary file (csv / sqlite / xlsx / unknown): present it through a
    # FilesystemContainer over its parent yielding just this one file, so single-file
    # input still produces a SourceFile.
    container = FilesystemContainer(input_path.parent)
    file = SourceFile(containers=(container,), logical_path=PurePosixPath(input_path.name))
    log.info(f"Discovered single file {input_path}")
    return (file,)


def _zip_namelist(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return []
