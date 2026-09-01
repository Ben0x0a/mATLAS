"""The app-layer pipeline: one ``process()`` run over the relational engine bricks.

Defines:    ``process`` — discover + persist L0 (``ingest.writer.ingest_input``), match
            declarative presets (``presets.matcher`` over a second, cheap walk),
            assemble L1 (``ingest.assemble.assemble_source``), integrate L2
            (``integrate.spatial`` / ``integrate.temporal``), normalise entities
            (``integrate.entity`` — MUST precede analysis, whose grouping key is the
            normalised identifier), analyse L3 (``analyse.untangle``) and run the
            selected export bricks (``export.registry``; the ``csv`` brick is the
            default) plus the traceability / warnings sidecars — and ``ProcessResult``,
            the stable result shape the GUI and CLI consume.
Used by:    ``gui.app`` (worker thread), ``launcher.cli`` and
            ``model_atlas.__init__`` (public API re-export).
Depends on: ``model_atlas.core`` (ids, determinism, store), ``model_atlas.ingest``
            (discover, writer, assemble, format_detect, containers),
            ``model_atlas.integrate``, ``model_atlas.analyse``,
            ``model_atlas.export`` (registry + sidecar writers),
            ``model_atlas.presets`` (spec_loader, matcher, spec, resolve),
            ``model_atlas.app`` (config, reporting).

This is the successor of the deleted flat-engine ``model_atlas/pipeline.py``.
``process()``'s public signature and the ``ProcessResult`` shape are kept EXACTLY as
the GUI/CLI consume them; what changed is everything behind them — the run now fills
one relational ``RunStore`` (L0..L3) and every output is a projection over it.

Design decisions
----------------
* **Two walks, one read.** ``ingest_input`` owns the walk that READS evidence (it
  fingerprints, materialises and persists every file it opens — the rows it writes are
  preset-free: a source is *what was opened*). Matching then re-walks the SAME container
  tree only to enumerate ``SourceFile``s for the path/name selectors; its column peeks are
  answered FROM THE STORE (the first raw_record's keys), so no source file is ever opened
  twice for reading.
* **L0 opens what the presets can target.** Preset-free describes the ROWS, not the scope:
  ingest is handed a target filter (``_preset_target_fn``) built from the matcher's own
  content-free stage, so a run over a full acquisition opens the handful of files its
  presets locate instead of every database and every table in the archive. Files passed
  over are counted in the ingest report, never silently dropped. The filter is exactly the
  matcher's steps 1-2, so it can never decline a file that would later have matched.
* **Store-backed peeks.** The matcher's two reader touch points (``peek``,
  ``subtables``) are injected closures over the run store, which guarantees matching
  sees exactly what ingest read — a separately-opened reader could disagree.
* **sql selectors are not assembled.** L0 persists TABLES; a ``sql:`` selector has no
  corresponding source row, so it is reported loudly as unsupported rather than
  silently dropped (the previous engine ran the query at extract time; running SQL over
  the verbatim raw_record layer is future work).
* **case_number.** The relational schema roots every run in a case. On a
  CREATE run the ``case`` label (falling back to ``base_name``) is stored as the
  case_number; on an APPEND run the case identity is READ from the existing DB, never
  re-derived, so a case keeps its identity across every run added to it.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from model_atlas import __version__
from model_atlas.analyse.untangle import analyse
from model_atlas.app import reporting
from model_atlas.app.config import get_settings
from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.determinism import canonical_path, container_inner_path
from model_atlas.core.store import RunStore
from model_atlas.export import traceability_path_for, warnings_path_for, write_json
from model_atlas.export.csv_flat import ExportContext
from model_atlas.export.registry import get_exporter, load_builtin_exporters, run_exporters
from model_atlas.ingest.assemble import assemble_source
from model_atlas.ingest.containers import SourceFile
from model_atlas.ingest.discover import FileFound, walk
from model_atlas.ingest.format_detect import detect_path_format, detect_source_format
from model_atlas.ingest.readers.base import RawRecord
from model_atlas.ingest.writer import (
    Target,
    TargetFn,
    ingest_input,
    is_matlas_run_db,
    opened_input_path,
)
from model_atlas.integrate.entity import normalise_entities
from model_atlas.integrate.spatial import integrate_spatial
from model_atlas.integrate.temporal import (
    TemporalExpressionBundle,
    integrate_expression,
    standard_methods,
)
from model_atlas.presets.matcher import (
    locate_candidates,
    match_files_detailed,
    resolve_force_selector,
)
from model_atlas.presets.spec import InputSelector, PresetSpec
from model_atlas.presets.spec_loader import load_preset_specs

log = logging.getLogger(__name__)

# The registry name of the primary flat-CSV brick; written when the caller selects no
# exports (the historic default). Consumed by: process / _run_exports below, gui.app.
PRIMARY_CSV_EXPORT_NAME = "csv"

# The sqlite/case-DB export brick name. The case database is written intrinsically (persist
# on create / commit on append), so this is filtered OUT of the selectable exports — a brick
# re-persisting to the same path would be redundant (create) or unsafe (append, in place).
SQLITE_EXPORT_NAME = "sqlite_db"

# A progress reporter is called at the START of each pipeline phase with a short human
# phase name (no percentage). The library stays UI-agnostic: the GUI passes a callback
# that emits a Qt signal.
ProgressFn = Callable[[str], None]


def _emit(progress: "ProgressFn | None", phase: str) -> None:
    """Log the phase start and notify the optional progress reporter. A reporter that
    raises must never break the run, so it is guarded."""
    log.info(f"{phase}...")
    if progress is not None:
        try:
            progress(phase)
        except Exception:  # noqa: BLE001 - progress reporting is best-effort, never fatal.
            log.debug("progress callback raised", exc_info=True)


@dataclass(frozen=True)
class ProcessResult:
    """The stable result shape ``gui.app`` / ``launcher.cli`` consume.

    ``output_case_db`` is the durable case database — always written (created or appended
    to), so it is the one output that is never None. ``output_csv`` and the other
    projections are written only when the run produced observations; each is a SINGLE file
    (one file per format — the merge/split modes were removed with the case model)."""

    output_case_db: Path                                    # the case DB — always written
    output_csv: Path | None = None                          # the flat CSV (if the csv brick ran)
    output_traceability: Path | None = None
    output_warnings: Path | None = None
    # mutable defaults — a shared object across instances would be a latent aliasing bug.
    row_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    output_extras: list[Path] = field(default_factory=list)  # written by additional exporters


# --- small derivations shared across the run ---------------------------------

def _file_identity(file: SourceFile) -> tuple[str, str | None]:
    """The (input_file_path, inner_path) pair of one walked file — the SAME derivation
    the L0 writer used, so ``ids.source_id`` re-derives the identical source row id."""
    input_file_path = canonical_path(str(opened_input_path(file).resolve()))
    inner_path = container_inner_path(file.inner_chain)
    return input_file_path, inner_path


def _file_label(file: SourceFile) -> str:
    """A human-readable locator for the matched/unmatched report lines: the
    chain-inclusive inner path for an archived file, the logical path otherwise."""
    chain = file.inner_chain
    return "!".join(chain) if chain else str(file.logical_path)


def _preset_row(spec: PresetSpec) -> V.Preset:
    """The store's ``preset`` row for a loaded spec: id from (key ⟂ version) exactly as
    ``assemble_source`` re-derives it, the display title as ``name``, and the YAML
    bytes' sha256 as the definition digest (detects an edited preset whose key/version
    was not bumped)."""
    return V.Preset(
        preset_id=ids.preset_id(spec.meta.id, spec.meta.version),
        key=spec.meta.id,
        name=spec.meta.title,
        version=spec.meta.version,
        definition_digest=hashlib.sha256(spec.path.read_bytes()).hexdigest(),
    )


def _source_units(specs: list[PresetSpec]) -> dict[str, str]:
    """part -> declared source unit (e.g. horizontal_speed_kmh -> m/s) across every
    spec — the reference data L2's UnitConvert needs to read a verbatim metric value
    into its canonical unit."""
    units: dict[str, str] = {}
    for spec in specs:
        for field_spec in spec.common:
            if field_spec.unit:
                units[field_spec.model_field] = field_spec.unit
        for template in spec.observations:
            for field_spec in template.fields:
                if field_spec.unit:
                    units[field_spec.model_field] = field_spec.unit
    return units


def _datetime_format(spec: PresetSpec) -> str | None:
    """The strptime format the spec's (single) time declares — the local_string decode
    instruction L2's TzLocalise needs; None for an epoch/ISO spec."""
    for template in spec.observations:
        for time_spec in template.temporal:
            return time_spec.format
    return None


def _duration_unit(spec: PresetSpec) -> str | None:
    """The SOURCE unit of the spec's ``duration_sec:`` bound — the instruction L2's
    start_plus_duration method needs to turn the verbatim span into an end bound. None when
    the spec states an ordinary ``upper:`` clock. Sibling of ``_datetime_format`` above: both
    read a decode instruction that lives in the preset, never in the store."""
    for template in spec.observations:
        for time_spec in template.temporal:
            if time_spec.duration_unit is not None:
                return time_spec.duration_unit
    return None


def _rows(store: RunStore, sql: str, *args: object) -> list[dict]:
    cursor = store.connection.execute(sql, args)
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _read_case_id(store: RunStore) -> str:
    """The case id of an opened case DB (one case per DB). A new run APPENDS
    under this id rather than minting a fresh case."""
    row = store.connection.execute("SELECT case_id FROM case_metadata LIMIT 1").fetchone()
    if row is None:
        raise ValueError("case database has no case_metadata row; not a valid case DB")
    return row[0]


# --- the store-backed matcher touch points -----------------------------------

def _preset_target_fn(specs: list[PresetSpec], root_prefix_depth: int) -> TargetFn:
    """The L0 target filter: which walked files the selected presets could possibly read.

    HOW: run the matcher's content-free stage (location pre-filter + format/namespace guard)
    over the file and keep the surviving selectors; a file with none is declined, and one with
    selectors is restricted to the sub-units they declare. A selector that names no sub-unit
    (``sql:``, or a whole-file format) widens the file back to every unit — the caller cannot
    know up front which table such a selector will touch.

    WHY the pre-filter is exactly the matcher's own first two steps: any file this declines
    would also have been declined at match time, so nothing that could produce an observation is
    passed over. Step 3 (the required-column gate) is the only stage needing records, and it
    only ever sees files this stage kept — its store-backed peek therefore never asks about a
    file ingest skipped.

    ``warn=False``: the mismatch warnings belong to the real matching pass, which runs later
    over the same files; emitting them here too would double every line."""
    def target(file: SourceFile, fmt: "str | None") -> "Target | None":
        candidates = locate_candidates(
            file, specs, root_prefix_depth=root_prefix_depth, detected=fmt, warn=False)
        if not candidates:
            return None
        read_params = _agreed_read_params(candidates, file)
        subunits = {selector.subunit() for _preset, selector in candidates}
        if None in subunits:
            return Target(subunits=None, read_params=read_params)
        return Target(subunits=frozenset(subunits), read_params=read_params)

    return target


def _agreed_read_params(candidates: list, file: SourceFile) -> dict:
    """The reader options the candidate selectors agree on — the file's declared dialect.

    HOW: collect each candidate selector's ``reader_params()`` and keep them only when every
    candidate declares the SAME thing; on a disagreement, warn and fall back to the reader
    defaults. WHY not "first one wins": L0 reads the file ONCE for all of them, so a silent
    pick would read the bytes under one preset's dialect and hand them to another's mapping —
    a mis-read that surfaces later as inexplicably absent fields, not as a dialect problem.
    """
    variants = {json.dumps(selector.reader_params(), sort_keys=True, default=str)
                for _preset, selector in candidates}
    if len(variants) == 1:
        return json.loads(variants.pop())
    log.warning(
        f"{file.logical_path}: the presets targeting this file declare different reader "
        f"options ({sorted(variants)}); reading with the reader defaults — give the "
        f"selectors the same delimiter/encoding/header_row/skip_rows")
    return {}


def _store_peek_fn(store: RunStore) -> Callable[[SourceFile, InputSelector], "set[str] | None"]:
    """A matcher ``peek`` answered from the run store: the first raw_record's keys.

    Returns None (— "cannot verify", which the matcher treats as no disqualification)
    for a ``sql:`` selector, a source ingest could not read, or an empty unit."""
    def peek(file: SourceFile, selector: InputSelector) -> "set[str] | None":
        if selector.sql:
            return None
        input_file_path, inner_path = _file_identity(file)
        source_id = ids.source_id(input_file_path, inner_path, selector.subunit())
        rows = _rows(
            store,
            "SELECT raw_json FROM raw_record WHERE source_id = ? ORDER BY line_no LIMIT 1",
            source_id,
        )
        if not rows:
            return None
        return set(json.loads(rows[0]["raw_json"]).keys())

    return peek


def _store_subtables_fn(store: RunStore) -> Callable[[SourceFile, InputSelector], "list[str]"]:
    """The matcher's force-mode sub-unit listing, answered from the run store: every
    persisted subunit of the file's source rows (tables / sheets / model types)."""
    def subtables(file: SourceFile, selector: InputSelector) -> list[str]:
        input_file_path, inner_path = _file_identity(file)
        rows = _rows(
            store,
            "SELECT subunit FROM source WHERE input_file_path = ? AND inner_path IS ?",
            input_file_path, inner_path,
        )
        return [row["subunit"] for row in rows if row["subunit"] is not None]

    return subtables


# --- L2 temporal integration (per preset — the decode config lives in the spec) ---

def _integrate_temporal_for_preset(store: RunStore, spec: PresetSpec, local_zone: str | None) -> None:
    """Integrate the not-yet-integrated temporal expressions this spec produced.

    HOW: re-read each temporal_expression reachable from this preset's observations plus
    its verbatim component rows, wrap them as a method bundle, and run the standard
    method set configured with the spec's own datetime format and the operator's
    configured local zone. WHY per preset (not one whole-store pass): the parsing
    format lives in the preset, not the store, so each preset's expressions must be
    integrated with THAT preset's decode configuration."""
    methods = standard_methods(zone=local_zone, datetime_format=_datetime_format(spec),
                               duration_unit=_duration_unit(spec))
    preset_id = ids.preset_id(spec.meta.id, spec.meta.version)
    expressions = _rows(
        store,
        "SELECT DISTINCT te.temporal_expression_id, te.kind, te.referential, "
        "te.temporal_source FROM temporal_expression te "
        "JOIN observation a ON a.temporal_expression_id = te.temporal_expression_id "
        "WHERE a.preset_id = ? AND te.temporal_expression_id NOT IN "
        "(SELECT temporal_expression_id FROM timespan_integration)",
        preset_id,
    )
    for expression in expressions:
        components = _rows(
            store,
            "SELECT temporal_component_id, temporal_expression_id, ordinal, part, "
            "stated_offset, source_field, raw_value FROM temporal_component "
            "WHERE temporal_expression_id = ? ORDER BY ordinal",
            expression["temporal_expression_id"],
        )
        bundle = TemporalExpressionBundle(
            expression=V.TemporalExpression(**expression),
            components=tuple(V.TemporalComponent(**component) for component in components),
        )
        integrate_expression(store, bundle, methods)


# --- the run -----------------------------------------------------------------

def process(
    input_path: Path,
    presets_path: Path,
    output: Path,
    *,
    concerns: str,
    base_name: str = "merged",
    case: str | None = None,
    traceability_format: str = "readable",
    entity: str | None = None,
    include_source_columns: bool = True,
    root_prefix_depth: int | None = None,
    max_container_depth: int | None = None,
    local_zone: str | None = None,
    exports: list[str] | None = None,
    progress: ProgressFn | None = None,
) -> ProcessResult:
    """Run the full relational pipeline (L0 -> L3 -> exports) over one input.

    Args:
        input_path:           Source file, folder or archive (CSV, Excel, SQLite, XML).
        presets_path:         Preset YAML file or folder (scanned recursively).
        output:               Output FOLDER. The case DB is written at
                              <output>/<base_name>.matlas.sqlite (created, or appended to
                              if it already exists); every other export is one file per
                              format beside it (<output>/<base_name>.<ext>).
        concerns:        Required. The entity linked to every observation (e.g. the
                              case subject); OVERRIDES any preset-mapped concerns. A
                              plain literal applies to every row; a call-form reference
                              such as ``field(Evidence number)`` resolves per source row.
        base_name:            Stem for the case DB and every output file. Default "merged".
        case:                 Optional human case label; stored as case_metadata.case_number
                              on CREATE (falls back to base_name). On APPEND it is unused —
                              the case identity is read from the existing DB.
        traceability_format:  "readable" (default) or "prov" (W3C PROV-JSON).
        entity:               Optional; OVERRIDES any preset-mapped entity.
        include_source_columns: True (default) -> the ``orig_<col>`` verbatim tail is
                              projected after the canonical columns.
        root_prefix_depth / max_container_depth / local_zone: per-run overrides of the
                              matlas_config.toml discovery/timezone settings.
        exports:              Registered export-brick names to run; None -> the primary
                              ``csv`` brick only.
        progress:             Optional per-phase callback (see ``ProgressFn``).
    """
    # --output is a FOLDER in both modes. A pre-existing FILE there can never be turned
    # into a folder, so fail before any processing rather than deep in mkdir after a run.
    output = Path(output)
    if output.exists() and output.is_file():
        raise ValueError(f"Output path {output} exists and is a file; --output must be a folder.")

    # Per-run args win; otherwise fall back to matlas_config.toml (or built-in defaults).
    settings = get_settings()
    if root_prefix_depth is None:
        root_prefix_depth = settings.discovery.root_prefix_depth
    if max_container_depth is None:
        max_container_depth = settings.discovery.max_container_depth
    if local_zone is None:
        local_zone = settings.timezone.local_zone

    started_at = reporting.now_iso()
    input_path = Path(input_path)

    _emit(progress, "Loading presets")
    specs = load_preset_specs(presets_path)
    preset_rows = tuple(_preset_row(spec) for spec in specs)

    # Force-preset mode: a single ordinary file (not an archive) + exactly one preset in
    # one YAML -> the preset applies WITHOUT path/name filtering; its format is still
    # verified and the in-file sub-unit still resolved (store-backed, below).
    input_is_file = input_path.is_file()
    input_is_archive = input_is_file and detect_path_format(input_path) == "archive"
    force_preset = (
        input_is_file and not input_is_archive
        and Path(presets_path).is_file() and len(specs) == 1
    )
    if force_preset:
        log.info(f"Force-preset mode: applying {specs[0].name!r} (preset selector bypassed)")

    warnings: list[str] = []
    matched: list[str] = []
    unmatched: list[str] = []

    # The case database is the durable artefact — one per output name. If it already exists,
    # this run APPENDS to that case (a new run under the same case_metadata); otherwise it
    # CREATES a new case. Append is deterministic: no run/case id feeds any evidence digest,
    # so the same evidence yields byte-identical ids/digests whichever run adds it.
    case_db_path = output / f"{base_name}.matlas.sqlite"
    is_append = case_db_path.is_file() and is_matlas_run_db(case_db_path)
    # Refuse to clobber a pre-existing file at the case-DB path that is NOT a mATLAS run DB
    # (an unrelated file, or a truncated/corrupt earlier run). On CREATE, store.persist()
    # unlinks whatever sits at the target before VACUUM INTO, so proceeding would SILENTLY
    # DELETE that file. A forensic tool must never destroy a file it does not recognise —
    # fail loudly and let the operator move it or choose another output folder / --name.
    if case_db_path.is_file() and not is_append:
        raise FileExistsError(
            f"{case_db_path} already exists but is not a mATLAS case database; refusing to "
            f"overwrite it. Move or remove it, or choose a different output folder or --name.")
    if is_append:
        store = RunStore.open_persisted(case_db_path)
        existing_case_id = _read_case_id(store)
        log.info(f"Appending a run to the existing case {case_db_path.name}")
    else:
        store = RunStore()
        existing_case_id = None

    try:
        # --- L0: one walk that READS the targeted evidence (preset-free rows) ----
        # The rows written stay preset-free (a source is what was opened: paths-only id, one
        # row per unit, no preset column). What the target filter decides is only WHICH files
        # the run opens — an operator choice, recorded in run.settings_json + run_preset.
        # Force mode ingests everything: it bypasses location matching by design, so a target
        # filter built from locations would decline the very file it must apply to.
        target = None if force_preset else _preset_target_fn(specs, root_prefix_depth)
        _emit(progress, "Discovering & ingesting sources")
        ingest_report = ingest_input(
            input_path, store,
            target=target,
            # On CREATE the case_number is the --case label (falling back to the run name);
            # on APPEND the case identity is read from the DB and this arg is unused.
            case_number=case or base_name,
            presets=preset_rows,
            tool_version=__version__,
            max_container_depth=max_container_depth,
            existing_case_id=existing_case_id,
        )
        warnings.extend(ingest_report.warnings)
        log.info(f"Ingested {ingest_report.sources} source(s), "
                 f"{ingest_report.records} record(s) from {input_path}"
                 + (f"; {ingest_report.not_targeted} file(s) targeted by no preset were not "
                    f"opened" if ingest_report.not_targeted else ""))

        # --- matching + L1/L2 per matched (file, preset) ----------------------
        _emit(progress, "Matching presets & assembling")
        peek = _store_peek_fn(store)
        subtables = _store_subtables_fn(store)
        files = [event.file for event in walk(input_path, max_container_depth=max_container_depth)
                 if isinstance(event, FileFound)]

        sources_meta: list[dict[str, Any]] = []
        # activity ids are record-scoped (preset-free), so ONE ordinal registry is
        # shared across every assemble call of the run (assemble_source's contract).
        activity_ordinals: dict[str, int] = {}

        for file in files:
            label = _file_label(file)
            if force_preset:
                selector = resolve_force_selector(
                    file, specs[0], detect_source_format(file),
                    subtables=subtables, peek=peek,
                )
                file_matches: list[tuple[PresetSpec, InputSelector]] = [(specs[0], selector)]
            else:
                file_matches, unsatisfied = match_files_detailed(
                    file, specs, root_prefix_depth=root_prefix_depth, peek=peek)
                # A unit that was located + type-matched but whose mapped columns are
                # absent cannot be faithfully mapped — fail it LOUDLY, then keep going.
                for preset, _selector, missing in unsatisfied:
                    message = (f"{label}: matched {preset.name} but required field(s) "
                               f"{sorted(missing)} are absent — not mapped (author a variant "
                               f"preset or mark them optional)")
                    log.error(message)
                    warnings.append(message)
                    unmatched.append(f"{label} -> {preset.name} [missing required: {sorted(missing)}]")
                if not file_matches:
                    if not unsatisfied:
                        log.debug(f"No preset matched {label}")
                        unmatched.append(label)
                    continue

            for preset, selector in file_matches:
                if _assemble_match(
                    store, file, preset, selector, label,
                    run_id=ingest_report.run_id,
                    entity=entity, concerns=concerns,
                    activity_ordinals=activity_ordinals,
                    warnings=warnings, matched=matched, unmatched=unmatched,
                    sources_meta=sources_meta,
                ):
                    _integrate_temporal_for_preset(store, preset, local_zone)

        # --- L2 spatial + entity normalisation + L3 ---------------------------
        _emit(progress, "Integrating & analysing")
        integrate_spatial(store, source_units=_source_units(specs))
        normalise_entities(store)
        # Untangle runs AFTER normalise_entities: its grouping key is the normalised
        # identifier, which the normaliser has just written onto the entity rows. On append
        # it re-ranks the WHOLE case (clear_existing), since a new row can join an existing
        # time group — the analysis is derived, so regenerating it is not an evidence edit.
        analyse(store, clear_existing=is_append)

        observation_count = _rows(store, "SELECT COUNT(*) AS n FROM observation")[0]["n"]
        ranked_count = _rows(
            store, "SELECT COUNT(*) AS n FROM analysis WHERE record_rank IS NOT NULL")[0]["n"]
        row_counts = {
            "sources": len(matched) + len(unmatched),
            "matched": len(matched),
            "rows": observation_count,
            "ranked": ranked_count,
        }

        # --- write the case DB (always) + additional exports + sidecars ------
        # The case database is the durable artefact and is ALWAYS written: a fresh copy on
        # create (VACUUM INTO), the in-place appended rows committed on append. Every other
        # export is a throwaway projection of the WHOLE case.
        output.mkdir(parents=True, exist_ok=True)
        if is_append:
            store.connection.commit()
        else:
            store.persist(case_db_path)

        output_csvs: list[Path] = []
        output_extras: list[Path] = []
        output_traceability: Path | None = None
        output_warnings_path: Path | None = None
        if observation_count:
            # sqlite is intrinsic (the case DB above), so it is never a selectable export
            # re-persisting to the same path.
            requested = list(exports) if exports is not None else [PRIMARY_CSV_EXPORT_NAME]
            export_names = [name for name in requested if name != SQLITE_EXPORT_NAME]
            _emit(progress, "Writing exports")
            load_builtin_exporters()   # self-contained: register every brick before running
            context = ExportContext(
                output_dir=output, base_name=base_name,
                include_source_columns=include_source_columns,
            )
            output_csvs, output_extras = _run_exports(store, export_names, context)
            if output_csvs or output_extras:
                # Sidecar file names anchor on the nominal primary-CSV path so they
                # exist (and are findable) even when the csv brick was deselected.
                anchor_csv = output / f"{base_name}.csv"
                content_csv = output_csvs[0] if output_csvs else None
                output_traceability, output_warnings_path = _write_sidecars(
                    anchor_csv, content_csv, started_at=started_at, input_path=input_path,
                    presets_path=Path(presets_path), row_counts=row_counts,
                    sources=sources_meta, warnings=warnings,
                    traceability_format=traceability_format,
                )
        else:
            log.info("No observations produced; the case database records the run.")
    finally:
        store.close()

    log.info("Processing finished.")
    return ProcessResult(
        output_case_db=case_db_path,
        output_csv=output_csvs[0] if output_csvs else None,
        output_traceability=output_traceability,
        output_warnings=output_warnings_path,
        row_counts=row_counts, warnings=warnings,
        matched=matched, unmatched=unmatched, output_extras=output_extras,
    )


def _assemble_match(
    store: RunStore,
    file: SourceFile,
    preset: PresetSpec,
    selector: InputSelector,
    label: str,
    *,
    run_id: str,
    entity: str | None,
    concerns: str | None,
    activity_ordinals: dict[str, int],
    warnings: list[str],
    matched: list[str],
    unmatched: list[str],
    sources_meta: list[dict[str, Any]],
) -> bool:
    """Assemble ONE matched (file, preset, selector) onto its persisted source row.

    Returns True when the source row existed and was assembled (so the caller runs the
    per-preset temporal integration), False when the match could not be served — a
    ``sql:`` selector (no persisted unit) or a source ingest could not read — which is
    reported loudly, never silently dropped.
    """
    if selector.sql:
        message = (f"{label}: preset {preset.name} uses a sql: selector, which the "
                   f"relational ingest does not persist as a source unit; not assembled")
        log.error(message)
        warnings.append(message)
        unmatched.append(f"{label} -> {preset.name} [sql selector unsupported]")
        return False

    input_file_path, inner_path = _file_identity(file)
    subunit = selector.subunit()
    source_id = ids.source_id(input_file_path, inner_path, subunit)
    source_rows = _rows(
        store,
        "SELECT source_id, run_id, fingerprint_sha256, record_count FROM source WHERE source_id = ?",
        source_id,
    )
    if not source_rows:
        message = (f"{label}: matched {preset.name} but no ingested source unit "
                   f"{subunit!r} exists (unreadable at ingest, or absent); not assembled")
        log.error(message)
        warnings.append(message)
        unmatched.append(f"{label} -> {preset.name} [source unit missing]")
        return False

    source = source_rows[0]
    # APPEND idempotency: this source is already in the case from an EARLIER run (ingest
    # skipped re-writing it, so the row still carries the prior run_id). Its observations already
    # exist — re-assembling would collide — so skip it. Not an error and not a new match: the
    # skip was already recorded at ingest, so nothing is added to matched/unmatched here.
    if source["run_id"] != run_id:
        log.info(f"{label}: {preset.name} — source already in the case; not re-assembled")
        return False
    records = [
        RawRecord(line_no=row["line_no"], fields=json.loads(row["raw_json"]))
        for row in _rows(
            store,
            "SELECT line_no, raw_json FROM raw_record WHERE source_id = ? ORDER BY line_no",
            source_id,
        )
    ]
    matched.append(f"{label} -> {preset.name}")

    report = assemble_source(
        records, preset, store,
        source_id=source_id,
        fingerprint_sha256=source["fingerprint_sha256"],
        source_file_name=file.name,
        source_file_path=str(file.logical_path),
        selector=selector,
        entity=entity,
        concerns=concerns,
        activity_ordinals=activity_ordinals,
    )
    warnings.extend(report.warnings)
    # assemble only REPORTS the source-grain data_source; the L0 owner —
    # this pipeline — sets it on the source row (digests are unaffected: data_source is
    # source-grain, outside both digests, so folder==zip parity holds).
    if report.data_source is not None:
        store.connection.execute(
            "UPDATE source SET data_source = ? WHERE source_id = ?",
            (report.data_source, source_id),
        )
    # source_tier is declared by the preset (the author knows the source's nature), but the
    # L0 writer is preset-free and defaults it to 'primary'; now that a preset has matched,
    # write its tier back. Also source-grain, outside both digests — parity is unaffected.
    store.connection.execute(
        "UPDATE source SET source_tier = ? WHERE source_id = ?",
        (preset.source_tier, source_id),
    )
    log.info(f"{label}: {len(records)} source row(s) -> {report.observation_count} observation(s)")

    source_fields = list(records[0].fields.keys()) if records else []
    sources_meta.append({
        "source_file": file.name,
        "raw_source_path": report.data_source or str(file.logical_path),
        "input_file_path": input_file_path,
        "input_file_name": Path(input_file_path).name,
        "container_chain": file.zip_labels,
        "format": selector.format,
        "table": selector.table,
        "sheet": selector.sheet,
        "source_fingerprint": source["fingerprint_sha256"],
        "preset_id": preset.meta.id,
        "matched_preset": preset.name,
        "parser": f"{preset.parser.name} {preset.parser.version}",
        "source_tier": preset.source_tier,
        "record_count": source["record_count"],
        "observation_count": report.observation_count,
        "frontier": reporting.frontier_report(preset, source_fields),
    })
    return True


def _run_exports(
    store: RunStore, export_names: list[str], context: ExportContext,
) -> tuple[list[Path], list[Path]]:
    """Run the selected export bricks, splitting the results into (csv paths, extras).

    The primary ``csv`` brick is invoked directly so its file can populate
    ``ProcessResult.output_csv``; every other selected brick runs through the registry
    (unknown names warn, a failing brick is logged and skipped — one bad exporter never
    aborts the run)."""
    log.info(f"Writing export(s): {', '.join(export_names)}")
    output_csvs: list[Path] = []
    if PRIMARY_CSV_EXPORT_NAME in export_names:
        spec = get_exporter(PRIMARY_CSV_EXPORT_NAME)
        assert spec is not None, "Invariant violated: the csv brick registers at import"
        try:
            output_csvs = spec.exporter.export(store, context)
        except Exception as exc:  # noqa: BLE001 - same resilience rule as run_exporters.
            log.error(f"Export 'csv' failed: {type(exc).__name__}: {exc}")
    extras = run_exporters(
        [name for name in export_names if name != PRIMARY_CSV_EXPORT_NAME], store, context)
    return output_csvs, extras


def _write_sidecars(
    anchor_csv: Path,
    content_csv: Path | None,
    *,
    started_at: str,
    input_path: Path,
    presets_path: Path,
    row_counts: dict[str, int],
    sources: list[dict[str, Any]],
    warnings: list[str],
    traceability_format: str,
) -> tuple[Path, Path]:
    """Write the traceability + warnings sidecars beside the (nominal) primary CSV."""
    # The run finish time, computed ONCE and shared by whichever traceability sidecar is
    # written, so a run's artefacts never disagree on when it finished (both formats stamped
    # their own now_iso() before, drifting by the milliseconds between the two calls).
    finished_at = reporting.now_iso()
    if traceability_format == "prov":
        trace = reporting.build_traceability_prov(
            started_at=started_at, finished_at=finished_at, output_csv=content_csv,
            sources=sources)
    else:
        trace = reporting.build_traceability_readable(
            started_at=started_at, finished_at=finished_at, input_path=input_path,
            presets_path=presets_path, output_csv=content_csv, row_counts=row_counts,
            sources=sources, warning_count=len(warnings),
        )
    output_traceability = write_json(traceability_path_for(anchor_csv), trace)
    output_warnings = write_json(
        warnings_path_for(anchor_csv), reporting.build_warnings_report(warnings, sources))
    return output_traceability, output_warnings
