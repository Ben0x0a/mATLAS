"""Container & SourceFile — a folder and an archive are the same thing.

Defines:    Container (Protocol), SourceFile, FilesystemContainer, ZipContainer.
            A Container is a namespace of files addressable by a logical path; it hands
            out cheap metadata (``files``), opens claimed files (``open``), and stages a
            throwaway copy with kind-aware integrity (``stage`` / ``stage_group``).
Used by:    discover (traversal), the FormatReaders (staging), the matcher (open+peek).
Depends on: integrity (sha256), sqlite.integrity (strategic zip fingerprint), staging.

WHY: nothing downstream should branch on "was this in an archive". Folder depth is
unbounded (a folder + all subfolders is ONE container); archive nesting is the only
thing ``max_container_depth`` bounds (see discover).
"""
from __future__ import annotations

import hashlib
import io
import logging
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable, Protocol, runtime_checkable

from model_atlas.integrity import sha256_file
from model_atlas.sources.staging import StagedFile, StagedGroup

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
DEFAULT_SIBLING_SUFFIXES = ("-wal", "-shm", "-journal")


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
    def full_logical_path(self) -> PurePosixPath:
        """Chain joined for traceability: ``archive/inner/path`` across nested levels."""
        parts: list[str] = []
        for container in self.containers[1:]:
            parts.append(container.root_label)
        parts.append(str(self.logical_path))
        return PurePosixPath(*parts) if parts else self.logical_path

    @property
    def container_chain(self) -> list[str]:
        """Archive names traversed, outermost->innermost, EXCLUDING the filesystem root.

        ``["EXTRACTION_FFS.zip"]`` for a zip entry, ``[]`` for a loose/folder file."""
        return [c.root_label for c in self.containers if isinstance(c, ZipContainer)]


@runtime_checkable
class Container(Protocol):
    root_label: str

    def files(self) -> Iterable[SourceFile]: ...
    def open(self, file: SourceFile) -> BinaryIO: ...
    def stage(self, file: SourceFile) -> StagedFile: ...
    def stage_group(
        self, file: SourceFile, sibling_suffixes: tuple[str, ...] = DEFAULT_SIBLING_SUFFIXES
    ) -> StagedGroup: ...
    def finalize(self, staged: StagedFile | StagedGroup) -> None: ...


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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

    def stage(self, file: SourceFile) -> StagedFile:
        original = self._abspath(file)
        before = sha256_file(original)
        temp_dir = Path(tempfile.mkdtemp(prefix="matlas-stage-"))
        dest = temp_dir / file.name
        shutil.copy2(original, dest)
        log.debug(f"Staged {original} -> {dest} (sha256={before})")
        return StagedFile(
            path=dest,
            fingerprint=before,
            origin=file,
            integrity={"mode": "full", "ok": None, "source_hash_before": before,
                       "verification_after": None},
            temp_dir=temp_dir,
        )

    def stage_group(
        self, file: SourceFile, sibling_suffixes: tuple[str, ...] = DEFAULT_SIBLING_SUFFIXES
    ) -> StagedGroup:
        original = self._abspath(file)
        temp_dir = Path(tempfile.mkdtemp(prefix="matlas-stage-"))
        members: dict[str, Path] = {}
        before_hashes: dict[str, str] = {}
        db_dest = temp_dir / file.name
        shutil.copy2(original, db_dest)
        members["db"] = db_dest
        before_hashes["db"] = sha256_file(original)
        for key, suffix in zip(("wal", "shm", "journal"), DEFAULT_SIBLING_SUFFIXES):
            if suffix not in sibling_suffixes:
                continue
            sib = original.with_name(file.name + suffix)
            if sib.exists():
                dest = temp_dir / (file.name + suffix)
                shutil.copy2(sib, dest)
                members[key] = dest
                before_hashes[key] = sha256_file(sib)
        return StagedGroup(
            dir=temp_dir,
            members=members,
            fingerprint=before_hashes["db"],
            origin=file,
            integrity={"mode": "full", "ok": None, "source_hashes_before": before_hashes,
                       "verification_after": None},
            temp_dir=temp_dir,
        )

    def finalize(self, staged: StagedFile | StagedGroup) -> None:
        """Re-hash the original(s) after the reader is done — proves we never wrote them."""
        if isinstance(staged, StagedFile):
            after = sha256_file(self._abspath(staged.origin))
            staged.integrity["ok"] = after == staged.integrity["source_hash_before"]
            staged.integrity["verification_after"] = {"sha256": after}
        else:
            before = staged.integrity["source_hashes_before"]
            original = self._abspath(staged.origin)
            after = {"db": sha256_file(original)}
            for key, suffix in zip(("wal", "shm", "journal"), DEFAULT_SIBLING_SUFFIXES):
                if key in before:
                    after[key] = sha256_file(original.with_name(staged.origin.name + suffix))
            staged.integrity["ok"] = after == before
            staged.integrity["verification_after"] = after


# --- Zip ------------------------------------------------------------------

class ZipContainer:
    """A zip archive presented as a Container. Built from a path (the input archive) or
    from raw bytes (a nested archive). The archive is never mutated during a run."""

    def __init__(self, *, path: Path | None = None, data: bytes | None = None, label: str | None = None) -> None:
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
        info = self._find_info(zf, file.logical_path)
        return zf.open(info)

    def _extract_entry(self, zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path) -> str:
        h = hashlib.sha256()
        with zf.open(info) as src, open(dest, "wb") as out:
            for chunk in iter(lambda: src.read(_CHUNK), b""):
                h.update(chunk)
                out.write(chunk)
        return h.hexdigest()

    def stage(self, file: SourceFile) -> StagedFile:
        temp_dir = Path(tempfile.mkdtemp(prefix="matlas-stage-"))
        dest = temp_dir / file.name
        with self._zipfile() as zf:
            info = self._find_info(zf, file.logical_path)
            content_sha = self._extract_entry(zf, info, dest)
        return StagedFile(
            path=dest,
            fingerprint=content_sha,
            origin=file,
            integrity={"mode": "strategic", "ok": True, "content_sha256": content_sha},
            temp_dir=temp_dir,
        )

    def stage_group(
        self, file: SourceFile, sibling_suffixes: tuple[str, ...] = DEFAULT_SIBLING_SUFFIXES
    ) -> StagedGroup:
        from model_atlas.sqlite.integrity import fingerprint_zip

        temp_dir = Path(tempfile.mkdtemp(prefix="matlas-stage-"))
        members: dict[str, Path] = {}
        found: dict[str, zipfile.ZipInfo] = {}
        with self._zipfile() as zf:
            db_info = self._find_info(zf, file.logical_path)
            found["db"] = db_info
            self._extract_entry(zf, db_info, temp_dir / file.name)
            members["db"] = temp_dir / file.name
            for key, suffix in zip(("wal", "shm", "journal"), DEFAULT_SIBLING_SUFFIXES):
                if suffix not in sibling_suffixes:
                    continue
                sib_path = file.logical_path.with_name(file.name + suffix)
                try:
                    info = self._find_info(zf, sib_path)
                except FileNotFoundError:
                    continue
                self._extract_entry(zf, info, temp_dir / (file.name + suffix))
                members[key] = temp_dir / (file.name + suffix)
                found[key] = info

        fingerprint = None
        integrity: dict
        if self.path is not None:
            zfp = fingerprint_zip(self.path, found)
            db_content = next(
                (e.content_sha256 for e in zfp.entries
                 if not e.arcname.endswith(DEFAULT_SIBLING_SUFFIXES)),
                zfp.entries[0].content_sha256 if zfp.entries else None,
            )
            fingerprint = db_content
            integrity = {"mode": "strategic", "ok": True,
                         "source_fingerprint_before": zfp.to_dict()}
        else:
            fingerprint = sha256_file(members["db"])
            integrity = {"mode": "strategic", "ok": True}
        return StagedGroup(
            dir=temp_dir,
            members=members,
            fingerprint=fingerprint,
            origin=file,
            integrity=integrity,
            temp_dir=temp_dir,
        )

    def finalize(self, staged: StagedFile | StagedGroup) -> None:
        """Verify the archive structure was not modified (strategic re-check)."""
        if isinstance(staged, StagedGroup) and self.path is not None:
            from model_atlas.sqlite.integrity import ZipFingerprint, verify_zip_fingerprint

            before = staged.integrity.get("source_fingerprint_before")
            if before is not None:
                fp = ZipFingerprint(
                    algorithm=before["algorithm"],
                    file_size=before["file_size"],
                    cd_offset=before["central_directory"]["offset"],
                    cd_size=before["central_directory"]["size"],
                    cd_sha256=before["central_directory"]["sha256"],
                )
                from model_atlas.sqlite.integrity import ZipEntryFingerprint
                fp.entries = [ZipEntryFingerprint(**e) for e in before["entries"]]
                report = verify_zip_fingerprint(fp, self.path)
                staged.integrity["ok"] = report.ok
                staged.integrity["verification_after"] = report.to_dict()
        # Single staged zip files keep the immediate ok=True (archive immutable).
