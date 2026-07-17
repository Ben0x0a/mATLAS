"""The app-layer pipeline: one ``process()`` run over the relational engine bricks.

Defines:    ``process`` — discover + persist L0 (``ingest.writer.ingest_input``), match
            declarative presets (``presets.matcher`` over a second, cheap walk),
            assemble L1 (``ingest.assemble.assemble_source``), integrate L2
            (``integrate.spatial`` / ``integrate.temporal``), normalise entities
            (``integrate.entity`` — MUST precede analysis, whose grouping key is the
            normalised identifier), analyse L3 (``analyze.untangle``) and run the
            selected export bricks (``export.registry``; the ``csv`` brick is the
            default) plus the traceability / warnings sidecars — and ``ProcessResult``,
            the stable result shape the GUI and CLI consume.
Used by:    ``gui.app`` (worker thread), ``launcher.cli`` and
            ``model_atlas.__init__`` (public API re-export).
Depends on: ``model_atlas.core`` (ids, determinism, store), ``model_atlas.ingest``
            (discover, writer, assemble, format_detect, containers),
            ``model_atlas.integrate``, ``model_atlas.analyze``,
            ``model_atlas.export`` (registry + sidecar writers),
            ``model_atlas.presets`` (spec_loader, matcher, spec, resolve),
            ``model_atlas.app`` (config, reporting).

This is the T60 successor of the deleted flat-engine ``model_atlas/pipeline.py``.
``process()``'s public signature and the ``ProcessResult`` shape are kept EXACTLY as
the GUI/CLI consume them; what changed is everything behind them — the run now fills
one relational ``RunStore`` (L0..L3) and every output is a projection over it.

Design decisions
----------------
* **Two walks, one read.** ``ingest_input`` owns the walk that READS evidence (it
  fingerprints, materialises and persists every readable file — L0 is preset-free,
  decision 27). Matching then re-walks the SAME container tree only to enumerate
  ``SourceFile``s for the path/name selectors; its column peeks are answered FROM THE
  STORE (the first raw_record's keys), so no source file is ever opened twice for
  reading.
* **Store-backed peeks.** The matcher's two reader touch points (``peek``,
  ``subtables``) are injected closures over the run store, which guarantees matching
  sees exactly what ingest read — a separately-opened reader could disagree.
* **sql selectors are not assembled.** L0 persists TABLES; a ``sql:`` selector has no
  corresponding source row, so it is reported loudly as unsupported rather than
  silently dropped (the pre-T60 engine ran the query at extract time; running SQL over
  the verbatim raw_record layer is future work).
* **case_number.** The relational schema roots every run in a case (decision 28) but
  ``process()``'s stable signature carries no case identity, so the input folder/file
  name stands in until the operator-facing case field lands.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from model_atlas import __version__
from model_atlas.analyze.untangle import analyse
from model_atlas.app import reporting
from model_atlas.app.config import get_settings
from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.determinism import canonical_path, container_inner_path
from model_atlas.core.store import RunStore
from model_atlas.export import traceability_path_for, warnings_path_for, write_json
from model_atlas.export.csv_flat import ExportContext
from model_atlas.export.registry import get_exporter, run_exporters
from model_atlas.ingest.assemble import assemble_source
from model_atlas.ingest.containers import SourceFile
from model_atlas.ingest.discover import FileFound, walk
from model_atlas.ingest.format_detect import detect_path_format, detect_source_format
from model_atlas.ingest.readers.base import RawRecord
from model_atlas.ingest.writer import ingest_input, opened_input_path
from model_atlas.integrate.entity import normalise_entities
from model_atlas.integrate.spatial import integrate_spatial
from model_atlas.integrate.temporal import (
    TemporalExpressionBundle,
    integrate_expression,
    standard_methods,
)
from model_atlas.presets.matcher import match_files_detailed, resolve_force_selector
from model_atlas.presets.spec import InputSelector, PresetSpec
from model_atlas.presets.spec_loader import load_preset_specs

log = logging.getLogger(__name__)

# The registry name of the primary flat-CSV brick; written when the caller selects no
# exports (the historic default). Consumed by: process / _run_exports below, gui.app.
PRIMARY_CSV_EXPORT_NAME = "csv"

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
    """The stable result shape ``gui.app`` / ``launcher.cli`` consume — field-for-field
    identical to the pre-T60 engine's ``ProcessResult``."""

    output_csv: Path | None                                 # set when merge=True
    output_traceability: Path | None = None                 # set when merge=True
    output_warnings: Path | None = None                     # set when merge=True
    output_csvs: list[Path] = field(default_factory=list)   # set when merge=False
    # mutable defaults — a shared object across instances would be a latent aliasing bug.
    row_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    output_extras: list[Path] = field(default_factory=list)  # written by additional exporters


# --- small derivations shared across the run ---------------------------------

def _selector_subunit(selector: InputSelector) -> str | None:
    """The store ``subunit`` a selector targets: the sqlite table / excel sheet / xml
    model type; None for csv (one implicit unit) AND for a ``sql:`` selector (which has
    no persisted unit — see the module header)."""
    if selector.format == "sqlite":
        return selector.table
    if selector.format == "excel":
        return selector.sheet
    if selector.format == "xml":
        return selector.model
    return None


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
    bytes' sha256 as the D40 change-detecting definition digest."""
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
        for template in spec.assertions:
            for field_spec in template.fields:
                if field_spec.unit:
                    units[field_spec.model_field] = field_spec.unit
    return units


def _datetime_format(spec: PresetSpec) -> str | None:
    """The strptime format the spec's (single) time declares — the local_string decode
    instruction L2's TzLocalise needs; None for an epoch/ISO spec."""
    for template in spec.assertions:
        for time_spec in template.temporal:
            return time_spec.format
    return None


def _rows(store: RunStore, sql: str, *args: object) -> list[dict]:
    cursor = store.connection.execute(sql, args)
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


# --- the store-backed matcher touch points -----------------------------------

def _store_peek_fn(store: RunStore) -> Callable[[SourceFile, InputSelector], "set[str] | None"]:
    """A matcher ``peek`` answered from the run store: the first raw_record's keys.

    Returns None (— "cannot verify", which the matcher treats as no disqualification)
    for a ``sql:`` selector, a source ingest could not read, or an empty unit."""
    def peek(file: SourceFile, selector: InputSelector) -> "set[str] | None":
        if selector.sql:
            return None
        input_file_path, inner_path = _file_identity(file)
        source_id = ids.source_id(input_file_path, inner_path, _selector_subunit(selector))
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

    HOW: re-read each temporal_expression reachable from this preset's assertions plus
    its verbatim component rows, wrap them as a method bundle, and run the standard
    method set configured with the spec's own datetime format and the operator's
    configured local zone. WHY per preset (not one whole-store pass): the parsing
    format lives in the preset, not the store, so each preset's expressions must be
    integrated with THAT preset's decode configuration."""
    methods = standard_methods(zone=local_zone, datetime_format=_datetime_format(spec))
    preset_id = ids.preset_id(spec.meta.id, spec.meta.version)
    expressions = _rows(
        store,
        "SELECT DISTINCT te.temporal_expression_id, te.kind, te.referential, "
        "te.temporal_source FROM temporal_expression te "
        "JOIN assertion a ON a.temporal_expression_id = te.temporal_expression_id "
        "WHERE a.preset_id = ? AND te.temporal_expression_id NOT IN "
        "(SELECT temporal_expression_id FROM timespan_integration)",
        preset_id,
    )
    for expression in expressions:
        components = _rows(
            store,
            "SELECT temporal_component_id, temporal_expression_id, ordinal, part, "
            "temporal_role, stated_offset, source_field, raw_value FROM temporal_component "
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
    linked_entity: str,
    base_name: str = "merged",
    traceability_format: str = "readable",
    merge: bool = True,
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
        output:               Output FOLDER (both modes). Merge mode writes one merged CSV
                              at <output>/<base_name>.csv; split mode one CSV per preset.
        linked_entity:        Required. The entity linked to every assertion (e.g. the
                              case subject); OVERRIDES any preset-mapped linked_entity. A
                              plain literal applies to every row; a call-form reference
                              such as ``field(Evidence number)`` resolves per source row.
        base_name:            Stem for run-level artefacts in merge mode. Default "merged".
        traceability_format:  "readable" (default) or "prov" (W3C PROV-JSON).
        merge:                True -> one merged CSV; False -> one CSV per preset.
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

    with RunStore() as store:
        # --- L0: one walk that READS everything (preset-free, decision 27) ----
        _emit(progress, "Discovering & ingesting sources")
        ingest_report = ingest_input(
            input_path, store,
            # The schema roots a run in a case (decision 28); process()'s stable
            # signature carries no case identity yet, so the input's name stands in.
            case_number=input_path.name or str(input_path),
            presets=preset_rows,
            tool_version=__version__,
            max_container_depth=max_container_depth,
        )
        warnings.extend(ingest_report.warnings)
        log.info(f"Ingested {ingest_report.sources} source(s), "
                 f"{ingest_report.records} record(s) from {input_path}")

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
                    entity=entity, linked_entity=linked_entity,
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
        # identifier, which the normaliser has just written onto the entity rows.
        analyse(store)

        assertion_count = _rows(store, "SELECT COUNT(*) AS n FROM assertion")[0]["n"]
        ranked_count = _rows(
            store, "SELECT COUNT(*) AS n FROM analysis WHERE record_rank IS NOT NULL")[0]["n"]
        row_counts = {
            "sources": len(matched) + len(unmatched),
            "matched": len(matched),
            "rows": assertion_count,
            "ranked": ranked_count,
        }

        # --- exports + sidecars ----------------------------------------------
        output_csvs: list[Path] = []
        output_extras: list[Path] = []
        output_traceability: Path | None = None
        output_warnings_path: Path | None = None
        if assertion_count:
            output.mkdir(parents=True, exist_ok=True)
            export_names = list(exports) if exports is not None else [PRIMARY_CSV_EXPORT_NAME]
            _emit(progress, "Writing exports")
            context = ExportContext(
                output_dir=output, base_name=base_name, merge=merge,
                include_source_columns=include_source_columns,
            )
            output_csvs, output_extras = _run_exports(store, export_names, context)
            if output_csvs or output_extras:
                # Sidecar file names anchor on the nominal primary-CSV path so they
                # exist (and are findable) even when the csv brick was deselected.
                anchor_csv = output / f"{base_name}.csv"
                content_csv = output_csvs[0] if merge and output_csvs else None
                output_traceability, output_warnings_path = _write_sidecars(
                    anchor_csv, content_csv, started_at=started_at, input_path=input_path,
                    presets_path=Path(presets_path), row_counts=row_counts,
                    sources=sources_meta, warnings=warnings,
                    traceability_format=traceability_format,
                )
        else:
            log.info("No assertions produced; no outputs written.")

    log.info("Processing finished.")
    if merge:
        return ProcessResult(
            output_csv=output_csvs[0] if output_csvs else None,
            output_traceability=output_traceability,
            output_warnings=output_warnings_path,
            row_counts=row_counts, warnings=warnings,
            matched=matched, unmatched=unmatched, output_extras=output_extras,
        )
    return ProcessResult(
        output_csv=None, output_csvs=output_csvs,
        output_traceability=output_traceability, output_warnings=output_warnings_path,
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
    entity: str | None,
    linked_entity: str | None,
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
    subunit = _selector_subunit(selector)
    source_id = ids.source_id(input_file_path, inner_path, subunit)
    source_rows = _rows(
        store,
        "SELECT source_id, fingerprint_sha256, record_count FROM source WHERE source_id = ?",
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
        linked_entity=linked_entity,
        activity_ordinals=activity_ordinals,
    )
    warnings.extend(report.warnings)
    # decision 24: assemble only REPORTS the source-grain data_source; the L0 owner —
    # this pipeline — sets it on the source row (digests are unaffected: data_source is
    # source-grain, outside both digests, so folder==zip parity holds).
    if report.data_source is not None:
        store.connection.execute(
            "UPDATE source SET data_source = ? WHERE source_id = ?",
            (report.data_source, source_id),
        )
    log.info(f"{label}: {len(records)} source row(s) -> {report.assertion_count} assertion(s)")

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
        "assertion_count": report.assertion_count,
        "frontier": reporting.frontier_report(preset, source_fields),
    })
    return True


def _run_exports(
    store: RunStore, export_names: list[str], context: ExportContext,
) -> tuple[list[Path], list[Path]]:
    """Run the selected export bricks, splitting the results into (csv paths, extras).

    The primary ``csv`` brick is invoked directly so its files can populate
    ``output_csv``/``output_csvs``; every other selected brick runs through the
    registry (unknown names warn, a failing brick is logged and skipped — one bad
    exporter never aborts the run)."""
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
    if traceability_format == "prov":
        trace = reporting.build_traceability_prov(
            started_at=started_at, output_csv=content_csv, sources=sources)
    else:
        trace = reporting.build_traceability_readable(
            started_at=started_at, input_path=input_path, presets_path=presets_path,
            output_csv=content_csv, row_counts=row_counts, sources=sources,
            warning_count=len(warnings),
        )
    output_traceability = write_json(traceability_path_for(anchor_csv), trace)
    output_warnings = write_json(
        warnings_path_for(anchor_csv), reporting.build_warnings_report(warnings, sources))
    return output_traceability, output_warnings
