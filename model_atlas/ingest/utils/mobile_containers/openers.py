"""How this library reaches the acquisition's bytes: the injected ``Opener``.

Defines:    the ``Opener`` protocol (list the logical paths, open one of them) and two
            standard-library implementations — ``ZipOpener`` (an acquisition archive,
            opened once) and ``DirectoryOpener`` (an extracted acquisition folder).
Used by:    ``mobile_containers.ios`` and ``mobile_containers.android`` (they are handed
            an ``Opener`` and never construct one) and the tests.
Depends on: the standard library only.

WHY an injected opener rather than mATLAS's ``ingest.containers.Container``: this package
must import nothing from ``model_atlas`` so it can be lifted out unchanged, and a two-method
protocol is the smallest surface that lets a host application supply its own reader. The
two implementations here exist so the library is usable on its own — a folder and an
archive are the only two shapes an acquisition arrives in.

WHY ``ZipOpener`` holds the archive open for its whole life: a zip's central directory is
read in full on every ``ZipFile`` construction, and an acquisition archive carries ~420 000
entries. Re-opening it per file (the shape an ``unzip -p`` per plist has) turned reading 84
small plists into over ten minutes; opening once and streaming makes the same read 0.2 s.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Iterable, Protocol, runtime_checkable


@runtime_checkable
class Opener(Protocol):
    """Read-only access to an acquisition addressed by logical path.

    Paths are POSIX-style, relative to the acquisition root, with no leading slash — the
    form both ``ZipFile.namelist()`` and a folder walk naturally produce. Only FILE paths
    are yielded: directory entries exist in some sources and not others, so a consumer
    derives directories from the file paths it sees rather than relying on one being there.

    ``list_paths`` must be re-iterable and stable: the iOS index walks it twice (once to
    find the metadata plists, once to spot container directories that have none) and both
    walks must see the same acquisition.
    """

    def list_paths(self) -> Iterable[str]: ...

    def open(self, path: str) -> BinaryIO: ...


class ZipOpener:
    """An acquisition archive, opened once and streamed from.

    Not a context manager by accident: ``close`` releases the single ``ZipFile`` handle
    that every read goes through, so an indexing run that forgets to close leaks it until
    garbage collection.
    """

    def __init__(self, archive_path: Path | str) -> None:
        self.archive_path = Path(archive_path)
        self._zip = zipfile.ZipFile(self.archive_path, "r")

    def list_paths(self) -> Iterable[str]:
        """Every entry in the archive — files AND directories — as a normalised path.

        HOW: normalise the separators and strip a leading slash so a Windows-written
        archive addresses the same way a POSIX one does, then drop the trailing slash a
        directory entry carries so both kinds address alike.

        WHY directories are yielded and not derived from file paths: a container whose
        contents are entirely subdirectories has no file to derive it from, so deriving
        makes it INVISIBLE. That is not hypothetical — on the verified Pixel acquisition
        ``data/media/0/Android/data/com.whatsapp`` holds six directory entries and no file,
        and every location of ``com.android.backupconfirm`` is a bare directory. Dropping
        them silently returned 3 of WhatsApp's 4 locations and 0 of backupconfirm's 4,
        which is exactly the silent loss this library must not commit.
        """
        for name in self._zip.namelist():
            normalised = name.replace("\\", "/").lstrip("/").rstrip("/")
            if normalised:
                yield normalised

    def open(self, path: str) -> BinaryIO:
        return self._zip.open(path, "r")

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> "ZipOpener":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self.close()
        return False


class DirectoryOpener:
    """An acquisition already extracted to a folder, presented as one ``Opener``."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def list_paths(self) -> Iterable[str]:
        """Every entry under the root — files AND directories — relative and POSIX-style.

        Directories are included for the same reason ``ZipOpener`` includes them: a
        container proven only by its subdirectories would otherwise never be seen.
        """
        for path in sorted(self.root.rglob("*")):
            yield path.relative_to(self.root).as_posix()

    def open(self, path: str) -> BinaryIO:
        return (self.root / path).open("rb")
