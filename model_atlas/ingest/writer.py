"""The L0 writer: walk an input and persist the acquisition layer into a RunStore.

Defines:    ``IngestReport`` (the counts a run produces), ``Target`` / ``TargetFn`` (what the
            injected preset-scoped filter says about one walked file: worth opening, which
            sub-units, and the READER OPTIONS its selectors declared — the file's dialect,
            which L0 needs before any preset has been matched), ``SelfIngestionError`` (raised
            when a persisted mATLAS run DB is fed back as evidence), ``is_matlas_run_db``
            (the self-ingestion stamp probe) and ``ingest_input`` — the single L0 entry point that writes
            ``case_metadata`` + ``run`` + ``preset``/``run_preset`` + ``archive``/
            ``run_archive`` + ``source`` + ``raw_record`` rows, in FK-safe order.
Used by:    the app-layer pipeline (once it exists) and tests/ingest/test_discover.py.
Depends on: ``model_atlas.ingest.discover`` (the walk events), ``model_atlas.ingest.
            containers`` (fingerprints + reader materialisation), ``model_atlas.ingest.
            format_detect`` (reader selection), ``model_atlas.ingest.readers`` (the
            format readers, imported for their registration side effect), and
            ``model_atlas.core`` (``ids``, ``vocabularies``, ``determinism``, ``schema``,
            ``store``).

WHY this is the L0 writer and not the old ``sources`` pipeline: the new engine's identity
discipline lives entirely in ``core`` (paths-only ids, the two evidence digests, the
canonical joins), so the acquisition layer is rebuilt here against ``core.store`` while the
old engine keeps running until the cut-over. The container *walk* is ported from
``sources/discover.py`` (see ``discover.py``); what is NEW is the persistence:

  * every archive encounter is recorded (``archive`` + ``run_archive``) — an
    archive id is derived from its PATH only (never the central-directory hash), so two
    byte-identical archives at two paths become two rows sharing ``cd_sha256`` (a
    GROUP BY finding, never an ingest-time merge);
  * an opt-in, preset-scoped skip still writes its ``run_archive`` link
    with ``skipped=1`` — never traceless — and only when the operator opted in AND a prior
    run parsed the same ``cd_sha256`` with the SAME preset set (key + version + digest);
  * a source id derives from PATHS ONLY (``input_file_path`` ⟂ ``inner_path`` ⟂ ``subunit``,
    §2c), so a folder file and the same file inside a zip share a
    ``fingerprint_sha256`` but stay two distinct source rows (the duplicate is a finding);
  * the fingerprint scope is the main-file bytes, with WAL/SHM/journal siblings hashed
    separately into ``sibling_fingerprints_json``;
  * the self-ingestion guard refuses a persisted ``*.matlas.sqlite`` top input and
    flags (skips) any such file discovered inside a folder/archive.
"""
from __future__ import annotations

import contextlib
import logging
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from model_atlas.core import ids
from model_atlas.core.determinism import (
    canonical_json, canonical_path, container_inner_path, source_order_json)
from model_atlas.core.schema import APPLICATION_ID
from model_atlas.core.store import RunStore
from model_atlas.core.vocabularies import (
    Archive,
    CaseMetadata,
    Preset,
    RawRecord,
    Run,
    RunArchive,
    RunPreset,
    SourceFile,
    SourceTier,
)
from model_atlas.ingest.containers import DiscoveredFile, ZipContainer
from model_atlas.ingest.discover import ArchiveFound, FileFound, walk
from model_atlas.ingest.format_detect import TARGETABLE_FORMATS, detect_source_format
from model_atlas.ingest.readers import Reader, get_reader

log = logging.getLogger(__name__)

_SQLITE_MAGIC = b"SQLite format 3\x00"
# application_id lives at byte offset 68 of the SQLite file header, big-endian (the file
# format spec) — reading it needs no open connection.
_APPLICATION_ID_OFFSET = 68


class SelfIngestionError(Exception):
    """Refused: the input is a persisted mATLAS run DB. A reproducible-scratch run
    DB must never be laundered back to L0 — its rows are derivations, not evidence."""


@dataclass(frozen=True)
class Target:
    """What a ``TargetFn`` says about ONE walked file: it is worth opening, and which of
    its sub-units are worth reading.

    ``subunits=None`` means EVERY sub-unit (the caller cannot name them up front — a
    ``sql:`` selector, or a format with no sub-unit concept); a frozenset names exactly
    the tables/sheets/models to read.

    ``read_params`` are the READER options the targeting selectors declared (a CSV's
    ``delimiter`` / ``encoding`` / ``header_row`` / ``skip_rows``). WHY they ride on the
    target rather than being looked up at read time: L0 runs before preset MATCHING, so the
    writer has no preset — but the target filter has already resolved which selectors could
    read this file, and those selectors are the only place the file's dialect is stated. An
    empty dict means "reader defaults", which is also what a disagreement between candidate
    selectors falls back to."""

    subunits: frozenset[str] | None = None
    read_params: dict[str, Any] = field(default_factory=dict)  # mutable default — shared if = {}


# The preset-scoped L0 target filter, INJECTED by the app layer: ``file -> Target | None``,
# where None means "no selected preset can target this file — do not open it". WHY injected
# rather than imported: presets depend on ingest, never the reverse, so the writer must not
# know what a selector is. WHY it exists at all: without it an L0 walk opens and fully reads
# every file of an acquisition (every table of every SQLite db) even when one preset targets
# one table — minutes of work for one file's worth of evidence.
#
# WHY it takes the file ALONE and not a pre-detected format: the filter's first stage is a
# content-free location test, so a file no selector's path can reach is declined without its
# bytes ever being touched. Handing it a format would mean sniffing every walked file up
# front — 512 bytes each, but one seek + one decompress-start per file inside an archive,
# which on a full-filesystem acquisition is a quarter of a million reads to answer a question
# the path already settled. The filter sniffs on its own, for the few files it located.
# None (no filter) reads every file: the writer's own default, and what the pipeline passes
# in force-preset mode, where location matching is bypassed by design.
TargetFn = Callable[[DiscoveredFile], "Target | None"]


@dataclass
class IngestReport:
    """The counts one ``ingest_input`` run produced — the writer's testable summary."""

    run_id: str
    case_id: str
    archives: int = 0
    archives_skipped: int = 0
    sources: int = 0
    records: int = 0
    skipped_unreadable: int = 0
    # files no selected preset could target — passed over unopened (never silently: the
    # count is reported and each path is logged).
    not_targeted: int = 0
    flagged_self_ingest: int = 0
    # sources already present in the case from an EARLIER run — skipped on append.
    sources_already_in_case: int = 0
    read_failures: int = 0
    # mutable default — a shared [] across instances would be a latent aliasing bug.
    warnings: list[str] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_matlas_run_db(path: Path) -> bool:
    """True when ``path`` is a persisted mATLAS run DB: the SQLite magic, the mATLAS
    ``application_id`` stamp, AND the ``matlas_meta`` marker table.

    WHY all three: a bare SQLite header or a coincidental application_id could occur in
    unrelated evidence; only the full set proves this file was minted by mATLAS, so the
    guard cannot false-positive on a genuine SQLite exhibit."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(_APPLICATION_ID_OFFSET + 4)
    except OSError:
        return False
    if head[:16] != _SQLITE_MAGIC or len(head) < _APPLICATION_ID_OFFSET + 4:
        return False
    app_id = int.from_bytes(head[_APPLICATION_ID_OFFSET:_APPLICATION_ID_OFFSET + 4], "big")
    if app_id != APPLICATION_ID:
        return False
    try:
        uri = f"file:{path}?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'matlas_meta'"
            ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _preset_signature(presets: tuple[Preset, ...]) -> str:
    """The canonical signature of a SELECTED preset set for skip eligibility.

    HOW: canonical JSON over the sorted ``(key, version, definition_digest)`` triples — a
    version bump OR an un-bumped definition edit changes the digest, which changes the
    signature. WHY the digest and not just the version: a skip must never happen when the
    definition changed silently under the same version, so the digest MUST participate."""
    triples = sorted([p.key, p.version, p.definition_digest] for p in presets)
    return canonical_json(triples)


def opened_input_path(file: DiscoveredFile) -> Path:
    """The outermost on-disk artefact opened for ``file``: the first archive with an
    on-disk path in its container chain, else the loose file's own path.

    Public: the app pipeline derives each source's id from the SAME opened path
    the writer used, so the two derivations can never drift."""
    for container in file.containers:
        if isinstance(container, ZipContainer) and container.path is not None:
            return container.path
    # No archive in the chain: a loose filesystem file — its innermost container is the
    # folder, whose ondisk_path IS the file itself.
    disk = file.container.ondisk_path(file)
    assert disk is not None, "a loose file must resolve to an on-disk path"
    return disk


def ingest_input(
    input_path: Path | str,
    store: RunStore,
    *,
    case_number: str,
    presets: tuple[Preset, ...] = (),
    examiner: str | None = None,
    exhibit_ref: str | None = None,
    title: str | None = None,
    description: str | None = None,
    tool_version: str | None = None,
    max_container_depth: int = 1,
    skip_reparsed_archives: bool = False,
    prior_archive_presets: dict[str, str] | None = None,
    existing_case_id: str | None = None,
    target: TargetFn | None = None,
) -> IngestReport:
    """Walk ``input_path`` and write its acquisition layer into ``store``.

    ``presets`` are the operator-SELECTED presets (recorded even when a preset
    yields nothing). ``skip_reparsed_archives`` + ``prior_archive_presets`` (a
    ``cd_sha256 -> preset signature`` map from earlier runs) drive the opt-in, preset-scoped
    archive skip. ``exhibit_ref`` and ``examiner`` are the optional
    chain-of-custody fields. Returns an ``IngestReport`` of the counts produced.

    ``existing_case_id`` switches CREATE vs APPEND: None (default) creates a fresh case rooted
    in this DB; a value adds a NEW run to that EXISTING case (the caller has opened a persisted
    case DB and read its case id). A run id is a fresh uuid4 that feeds no child id, so the same
    evidence appended later produces byte-identical downstream ids/digests — appending is
    deterministic, and a duplicate source collides on its content-derived id and is rejected
    exactly as in a fresh run.

    ``target`` is the optional preset-scoped L0 filter (see ``TargetFn``): a file it declines
    is passed over unopened and counted in ``IngestReport.not_targeted``; a file it accepts is
    read as usual, restricted to the sub-units it names. None (the default) reads everything.
    """
    input_path = Path(input_path)
    prior_archive_presets = prior_archive_presets or {}

    # refuse a persisted run DB fed straight back in as the top input (never laundered).
    if input_path.is_file() and is_matlas_run_db(input_path):
        raise SelfIngestionError(
            f"input {input_path} is a persisted mATLAS run DB (application_id + matlas_meta); "
            f"refusing to re-ingest a reproducible-scratch DB as L0 evidence"
        )

    # --- case + run: the run row carries the COMPLETE operator settings.
    # CREATE mints the case root; APPEND reuses the existing case (the caller read its id from
    # the opened DB) and only adds a run — re-writing case_metadata would collide on its PK.
    if existing_case_id is None:
        case = CaseMetadata(
            case_id=ids.case_metadata_id(), case_number=case_number, title=title,
            description=description, created_at=_now_iso(),
        )
        store.write_case_metadata(case)
        case_id = case.case_id
    else:
        case_id = existing_case_id

    current_signature = _preset_signature(presets)
    settings = {
        "input_path": str(input_path),
        "max_container_depth": max_container_depth,
        "skip_reparsed_archives": skip_reparsed_archives,
        # decision 39: the run is reproducible from its row, so whether L0 was narrowed to
        # what the presets target — which decides what the case DB contains — is recorded.
        "preset_targeted_ingest": target is not None,
        "exhibit_ref": exhibit_ref,
        "examiner": examiner,
        "presets": [
            {"key": p.key, "version": p.version, "definition_digest": p.definition_digest}
            for p in presets
        ],
    }
    run = Run(
        run_id=ids.run_id(), case_id=case_id, input_path=str(input_path),
        started_at=_now_iso(), finished_at=None, tool_version=tool_version,
        settings_json=canonical_json(settings), examiner=examiner,
    )
    store.write_run(run)
    report = IngestReport(run_id=run.run_id, case_id=case_id)

    # --- preset + run_preset: every selected preset, recorded up front.
    for preset in presets:
        preset_id = store.write_preset(preset)   # SELECT-then-INSERT; raises on an un-bumped edit
        store.write_run_preset(
            RunPreset(
                run_preset_id=ids.run_preset_id(run.run_id, preset_id),
                run_id=run.run_id, preset_id=preset_id,
            )
        )

    def should_skip(cd_sha256: str) -> bool:
        """Skip only when opted in AND a prior run parsed this cd with the SAME preset set."""
        if not skip_reparsed_archives:
            return False
        return prior_archive_presets.get(cd_sha256) == current_signature

    written_archive_ids: set[str] = set()
    for event in walk(
        input_path, max_container_depth=max_container_depth, should_skip=should_skip
    ):
        if isinstance(event, ArchiveFound):
            _write_archive(store, run.run_id, event, written_archive_ids, report)
        else:
            assert isinstance(event, FileFound), "walk yields only ArchiveFound / FileFound"
            _ingest_file(store, run.run_id, event.file, exhibit_ref, report, target)

    if report.not_targeted:
        log.info(f"{report.not_targeted} file(s) targeted by no selected preset were not opened")
    return report


def _write_archive(
    store: RunStore, run_id: str, event: ArchiveFound, written: set[str], report: IngestReport,
) -> None:
    """Record one archive encounter: the ``archive`` row (path-keyed) + a ``run_archive``
    link. The archive row is written once per path (UNIQUE(path)); the link
    marks whether this run skipped it (opt-in, preset-scoped).

    Cross-run idempotency (mirrors ``_ingest_subunit``'s source-level guard): the ``archive``
    row is path-keyed, so re-appending the SAME archive to an existing case already has its
    row. A plain INSERT would then collide on UNIQUE(path) and ABORT the whole append BEFORE
    the source-level guard runs — breaking append of any zip/UFDR input. So the row is written
    only when absent; the ``run_archive`` link is ALWAYS written (its id carries the fresh
    ``run_id``, so it is new every run and never collides)."""
    archive_id = ids.archive_id(event.path)
    report.archives += 1
    if event.skipped:
        report.archives_skipped += 1
    if archive_id in written:
        return  # already recorded in THIS run (a within-run duplicate encounter)
    written.add(archive_id)
    # A real cross-run existence check keyed on archive identity (path-derived), not the
    # in-memory ``written`` set alone — the set is empty at the start of every run.
    already_present = store.connection.execute(
        "SELECT 1 FROM archive WHERE archive_id = ?", (archive_id,)).fetchone()
    if already_present is None:
        store.write_archive(
            Archive(archive_id=archive_id, path=event.path, cd_sha256=event.cd_sha256)
        )
    store.write_run_archive(
        RunArchive(
            run_archive_id=ids.run_archive_id(run_id, archive_id),
            run_id=run_id, archive_id=archive_id, parsed_at=_now_iso(),
            skipped=1 if event.skipped else 0,
        )
    )


def _ingest_file(
    store: RunStore, run_id: str, file: DiscoveredFile, exhibit_ref: str | None,
    report: IngestReport, target: TargetFn | None = None,
) -> None:
    """Open one leaf file as one-or-more sources (one per subunit) and write its records."""
    # The preset-scoped filter runs FIRST — before the format sniff, before the no-reader
    # report below, and before any hashing/materialisation — so an acquisition's thousands of
    # untargeted files are never opened at all, and produce a count rather than a warning per
    # file. WHY it may run at all: which files a run OPENS is an operator choice recorded in
    # run.settings_json + run_preset; what a source IS once opened stays preset-free
    # (paths-only id, one row per unit) — decision 27 governs identity, not scope.
    wanted = target(file) if target is not None else Target(subunits=None)
    if wanted is None:
        report.not_targeted += 1
        log.debug("not targeted by any selected preset: %s", file.logical_path)
        return

    fmt = detect_source_format(file)
    if fmt not in TARGETABLE_FORMATS:
        # plist / archive-at-depth / unknown: no reader ships, so nothing is read. Surfaced
        # (a run must never silently drop an input) but not an error — it is simply opaque.
        report.skipped_unreadable += 1
        message = f"no reader for {file.logical_path} (format {fmt!r}); left unread"
        log.warning(message)
        report.warnings.append(message)
        return

    input_file_path = canonical_path(str(opened_input_path(file).resolve()))
    inner_path = container_inner_path(file.inner_chain)
    fingerprint = file.container.content_sha256(file)
    siblings = file.container.sibling_sha256(file)
    sibling_json = canonical_json(siblings) if siblings else None

    with tempfile.TemporaryDirectory(prefix="matlas-ingest-") as tmp:
        path = file.container.materialise(file, Path(tmp))
        # a run DB discovered inside a folder/archive is flagged and skipped (not the
        # whole-run refusal a top input gets) — it is still never re-ingested.
        if fmt == "sqlite" and is_matlas_run_db(path):
            report.flagged_self_ingest += 1
            message = f"flagged self-ingestion: {file.logical_path} is a mATLAS run DB; skipped"
            log.warning(message)
            report.warnings.append(message)
            return

        reader = get_reader(fmt)
        subunits = _subunits_to_read(reader, path, file, wanted, report)
        for subunit in subunits:
            _ingest_subunit(
                store, run_id, reader, path, subunit, file, input_file_path, inner_path,
                fingerprint, sibling_json, exhibit_ref, report, wanted.read_params,
            )


def _subunits_to_read(
    reader: Reader, path: Path, file: DiscoveredFile, wanted: Target, report: IngestReport,
) -> list[str | None]:
    """The sub-units of one opened file this run reads: those the target names and the file
    actually has, or every one it has when the target names none.

    WHY the ``[None]`` fallback is format-aware: a reader that REQUIRES a sub-unit (sqlite
    needs a table, excel a sheet) cannot read ``subunit=None`` — handing it None produced a
    contract ValueError reported as an unreadable-evidence warning, hiding the real cause
    (an encrypted or corrupt db the probe could not open). Only a whole-file format (csv)
    legitimately reads as a single None unit."""
    available = reader.subunits(path)
    if not available:
        # getattr, not attribute access: readers are STRUCTURAL implementations of the Reader
        # protocol (a brick registers a plain class, it does not subclass), so a reader added
        # later does not inherit the protocol's default — and a missing declaration must mean
        # "whole-file format", the pre-existing behaviour, not an AttributeError mid-run.
        if getattr(reader, "requires_subunit", False):
            report.skipped_unreadable += 1
            message = (f"no readable {reader.format} sub-unit in {file.logical_path} "
                       f"(encrypted, corrupt or empty); left unread")
            log.warning(message)
            report.warnings.append(message)
            return []
        return [None]
    if wanted.subunits is None:
        return list(available)
    # A named sub-unit the file does not have is NOT reported here: preset matching is what
    # decides whether a preset applies, and it reports its own misses with the preset name.
    return [unit for unit in available if unit in wanted.subunits]


def _ingest_subunit(
    store: RunStore, run_id: str, reader: Reader, path: Path, subunit: str | None,
    file: DiscoveredFile, input_file_path: str, inner_path: str | None,
    fingerprint: str, sibling_json: str | None, exhibit_ref: str | None,
    report: IngestReport, read_params: dict[str, Any] | None = None,
) -> None:
    """Write one ``source`` row (paths-only id) plus its verbatim ``raw_record`` rows."""
    # APPEND idempotency: a source already in the case from an EARLIER run must not be
    # re-ingested — its observations already exist and a second write would collide. Skip it and
    # RECORD the skip (no silent loss); the assemble loop skips it too (run_id mismatch). A
    # within-run duplicate (same run_id) is a DIFFERENT matter: the discover dedup should have
    # prevented it, so it falls through to write_source_file and raises, surfacing the breach.
    #
    # Looked up by the NATURAL KEY, not by re-deriving the id: evidence-chain ids are random
    # (decision 50). UNIQUE(run_id, input_file_path, inner_path, subunit) is what the schema
    # enforces, and it is the same tuple, so this check is exactly as strong as before.
    existing = store.connection.execute(
        "SELECT run_id FROM source_file "
        "WHERE input_file_path = ? AND inner_path IS ? AND subunit IS ?",
        (input_file_path, inner_path, subunit)).fetchone()
    if existing is not None and existing[0] != run_id:
        report.sources_already_in_case += 1
        message = (f"{file.logical_path} (subunit {subunit!r}) is already in the case from an "
                   f"earlier run; skipped (nothing to add)")
        log.info(message)
        report.warnings.append(message)
        return

    source_file_id = ids.new_id()

    try:
        # The selectors' declared dialect (a CSV's delimiter/encoding/header_row/skip_rows).
        # Without it a ';'-delimited or BOM-less-latin1 export is read as ONE column whose
        # name is the whole header line, and every mapped field then resolves to nothing.
        records = list(reader.iter_records(path, subunit, read_params or {}))
    except Exception as exc:  # noqa: BLE001 — malformed evidence must not abort the run
        report.read_failures += 1
        message = f"could not read {file.logical_path} (subunit {subunit!r}): {exc}"
        log.warning(message)
        report.warnings.append(message)
        records = []

    store.write_source_file(
        SourceFile(
            source_file_id=source_file_id, run_id=run_id, input_file_path=input_file_path,
            inner_path=inner_path, reported_data_source=None,
            format=reader.format, subunit=subunit,
            fingerprint_sha256=fingerprint, sibling_fingerprints_json=sibling_json,
            # L0 is preset-free, so tier defaults to 'primary'; the app writes the matched
            # preset's declared tier back onto the source row after matching.
            exhibit_ref=exhibit_ref, source_tier=SourceTier.PRIMARY, record_count=len(records),
        )
    )
    report.sources += 1

    for record in records:
        store.write_raw_record(
            RawRecord(
                raw_record_id=ids.new_id(),
                source_file_id=source_file_id, line_no=record.line_no,
                # SOURCE column order, not sorted: the exported orig_<col> passthrough follows
                # the source's own order (feeds no digest — safe to differ from canonical).
                raw_json=source_order_json(record.fields),
                recovery_state=record.recovery_state,
                recovery_source=record.recovery_source,
            )
        )
    report.records += len(records)
