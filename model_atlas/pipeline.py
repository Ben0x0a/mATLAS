"""Declarative processing pipeline.

Defines:    process — discover sources, match declarative presets, extract via the
            adapter registry, assemble assertion rows, untangle, run the selected
            exporters (the ``csv`` exporter is the default) plus traceability
            (readable or PROV-JSON) and warnings+frontier sidecars. ``output`` is a
            FOLDER in both modes; ``base_name`` (default "merged") is the stem for
            run-level artefacts.
            merge=True  → one merged CSV at <output>/<base_name>.csv.
            merge=False → one CSV per matched preset written into <output>.
Used by:    the CLI and tests.
Depends on: sources (discovery + adapter registry), presets.spec_loader, matcher,
            transforms.assemble + rank, model.store (the one run-wide RunStore), reporting,
            export, exporters (csv + extras).
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from model_atlas import __version__, reporting
from model_atlas.config import Settings, get_settings
from model_atlas.export import traceability_path_for, warnings_path_for, write_json
from model_atlas.exporters import ExportContext, load_plugins, run_exporters
from model_atlas.model.families import OUTPUT_COLUMNS
from model_atlas.model.store import RunStore
from model_atlas.presets.matcher import match_files_detailed, resolve_force_selector
from model_atlas.presets.spec import InputSelector
from model_atlas.presets.spec_loader import load_preset_specs
from model_atlas.sources import SingleSourceExtractor, SourceFile, ZipContainer, discover
from model_atlas.sources.container import FilesystemContainer
from model_atlas.sources.format_detect import detect_path_format, detect_source_format
from model_atlas.sources.readers import get_reader
from model_atlas.transforms.assemble import BuildEnv, build_assertions, to_records
from model_atlas.transforms.rank import untangle

log = logging.getLogger(__name__)

# Fixed namespace for the deterministic per-run id (uuid5 over ``started_at|input_path``),
# mirroring the uuid5 id scheme used across the model. Consumed by: process (the run row).
_RUN_NAMESPACE = uuid.UUID("f2e1d0c3-4b5a-5968-8776-a5b4c3d2e1f0")

# The registered exporter that writes the primary flat-assertion CSV (model_atlas.exporters.builtin).
# When the caller selects no exports, this one is written — the current default behaviour.
PRIMARY_CSV_EXPORT_NAME = "csv"

# A progress reporter is called at the START of each pipeline phase with a short human phase name
# (no percentage). The library stays UI-agnostic: the GUI passes a callback that emits a Qt signal.
ProgressFn = Callable[[str], None]


def _emit(progress: "ProgressFn | None", phase: str) -> None:
    """Log the phase start and notify the optional progress reporter. A reporter that raises must
    never break the run, so it is guarded."""
    log.info(f"{phase}...")
    if progress is not None:
        try:
            progress(phase)
        except Exception:  # noqa: BLE001 - progress reporting is best-effort, never fatal.
            log.debug("progress callback raised", exc_info=True)


def _input_file_path(file: SourceFile) -> str:
    """Full path of the OUTERMOST on-disk artifact matlas opened: the input archive when
    reading from one (outermost if nested — including a zip sitting inside an input
    folder), else the leaf file's own filesystem path."""
    for container in file.containers:                       # outermost .. innermost
        if isinstance(container, ZipContainer) and container.path is not None:
            return str(container.path)
    top = file.containers[0]
    if isinstance(top, FilesystemContainer):
        return str(top.root.joinpath(*file.logical_path.parts))
    return str(file.logical_path)


def _peek(file: SourceFile, selector: InputSelector) -> "set[str] | None":
    return get_reader(selector.format).peek_fields(file, selector)


# Characters that are unsafe in filenames on any major OS.
_UNSAFE_CHARS = re.compile(r'[^\w.\-]')


def _safe_filename(name: str) -> str:
    return _UNSAFE_CHARS.sub('_', name)


@dataclass(frozen=True)
class ProcessResult:
    output_csv: Path | None                                 # set when merge=True
    output_traceability: Path | None = None                 # set when merge=True
    output_warnings: Path | None = None                     # set when merge=True
    output_csvs: list[Path] = field(default_factory=list)   # set when merge=False
    row_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    output_extras: list[Path] = field(default_factory=list)  # files written by additional exporters


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
    """Run the full pipeline.

    Args:
        input_path:           Source file or folder (CSV, Excel, SQLite).
        presets_path:         Preset YAML file or folder (scanned recursively).
        output:               Output FOLDER (both modes). Merge mode writes one merged CSV
                              at <output>/<base_name>.csv; split mode writes one CSV per
                              matched preset inside <output>.
        linked_entity:        Required. The entity linked to every output row (e.g. the
                              case subject). When supplied it OVERRIDES any linked_entity
                              a preset maps; a preset value is the default only when no
                              caller value reaches here. Declared as a required keyword
                              so the package API makes the obligation explicit. A plain
                              literal applies to every row; a preset-style reference such
                              as ``field(Evidence number)`` resolves per source row.
        base_name:            Stem for run-level artefacts in merge mode: the merged CSV
                              becomes <output>/<base_name>.csv and its sidecars share the
                              stem. Default "merged". Ignored in split mode (each preset's
                              file is named after the preset).
        traceability_format:  "readable" (default) or "prov" (W3C PROV-JSON).
        merge:                True → one merged output CSV (default).
                              False → one CSV per matched preset inside *output*.
        entity:               Optional entity. When supplied it OVERRIDES any entity the
                              preset maps; the preset value is the default used otherwise.
        include_source_columns: True (default) → append every source column verbatim as
                              an ``orig_<col>`` column after the canonical schema. False →
                              canonical columns only.
    """
    # --output is a FOLDER in both modes. A pre-existing FILE there can never be turned into a
    # folder, so fail before any processing rather than crashing deep in mkdir after a long run.
    output = Path(output)
    if output.exists() and output.is_file():
        raise ValueError(
            f"Output path {output} exists and is a file; --output must be a folder."
        )

    # Per-run args win; otherwise fall back to matlas_config.toml (or built-in defaults).
    settings = get_settings()
    if root_prefix_depth is None:
        root_prefix_depth = settings.discovery.root_prefix_depth
    if max_container_depth is None:
        max_container_depth = settings.discovery.max_container_depth
    if local_zone is None:
        local_zone = settings.timezone.local_zone
    # Register any drop-in exporters before they might be selected (missing folder => builtins only).
    load_plugins(Path(settings.export.plugins_dir))

    started_at = reporting.now_iso()
    # Announce each phase as it STARTS, not only on completion: discovery and extraction of a
    # large acquisition can take minutes, and without a start line the run looks stalled.
    _emit(progress, "Loading presets")
    presets = load_preset_specs(presets_path)
    _emit(progress, "Discovering sources")
    files = discover(input_path, max_container_depth=max_container_depth)
    log.info(f"Discovered {len(files)} source file(s); {len(presets)} preset(s) loaded")

    # Force-preset mode: the input is a single ordinary file (its detected format is NOT
    # an archive) and the presets argument is one YAML with exactly one preset. Then the
    # preset is applied to that one file WITHOUT path/name matching — but its `format` is
    # still verified (hard error on mismatch) and the in-file sub-unit still resolved. A
    # single zip/folder is a Container -> normal matching.
    input_is_file = Path(input_path).is_file()
    input_is_archive = input_is_file and detect_path_format(Path(input_path)) == "archive"
    force_preset = (
        input_is_file
        and not input_is_archive
        and Path(presets_path).is_file()
        and len(presets) == 1
    )
    if force_preset:
        log.info(f"Force-preset mode: applying {presets[0].name!r} (preset selector bypassed)")

    # ONE run-wide store per run (Phase C): build_assertions fills it per matched source; the
    # flat frame, the ranks and the persisted ``sqlite`` export all read from this single store.
    store = RunStore()
    run_id = str(uuid.uuid5(_RUN_NAMESPACE, f"{started_at}|{input_path}"))
    store.add_run(
        run_id=run_id, started_at=started_at, tool_version=__version__,
        settings_json=json.dumps(settings.to_traceability(), ensure_ascii=False, default=str),
    )

    extractor = SingleSourceExtractor()
    # sources_by_preset preserves insertion order so per-preset CSVs are written in the order
    # presets were first matched; preset_ids_by_name maps each preset name to the id(s) used to
    # slice the run-wide flat frame back into per-preset units for split mode.
    sources_by_preset: dict[str, list[dict]] = {}
    preset_ids_by_name: dict[str, set[str]] = {}
    warnings: list[str] = []
    matched: list[str] = []
    unmatched: list[str] = []

    _emit(progress, "Extracting & mapping")
    for file in files:
        label = str(file.full_logical_path)
        if force_preset:
            selector = resolve_force_selector(file, presets[0], detect_source_format(file))
            file_matches = [(presets[0], selector)]
        else:
            # A file may feed several presets — one per distinct in-file unit (e.g. one
            # preset per Cellebrite model type, or per SQLite table).
            file_matches, unsatisfied = match_files_detailed(
                file, presets, root_prefix_depth=root_prefix_depth, peek=_peek)
            # A unit that was located + type-matched but whose mapped columns are absent
            # cannot be faithfully mapped — fail it LOUDLY (per source), then keep going.
            for preset, selector, missing in unsatisfied:
                msg = (f"{label}: matched {preset.name} but required field(s) "
                       f"{sorted(missing)} are absent — not mapped (author a variant preset "
                       f"or mark them optional)")
                log.error(msg)
                warnings.append(msg)
                unmatched.append(f"{label} -> {preset.name} [missing required: {sorted(missing)}]")
            if not file_matches:
                if not unsatisfied:
                    log.debug(f"No preset matched {label}")
                    unmatched.append(label)
                continue
        for preset, selector in file_matches:
            if len(preset.roles) > 1:
                raise NotImplementedError("multi-source python extract not yet implemented")
            matched.append(f"{label} -> {preset.name}")
            extracted = extractor.extract({selector.role: (file, selector)}, preset)
            records = to_records(extracted.dataframe)
            env = BuildEnv(
                input_file_path=_input_file_path(file),
                input_file_name=Path(_input_file_path(file)).name,
                source_fingerprint=extracted.source_fingerprint,
                source_file_path=extracted.source_original_path,
                raw_source_path=extracted.source_original_path,
                source_tier=preset.source_tier,
                entity=entity,
                linked_entity=linked_entity,
                source_file_name=file.name,
                local_zone=local_zone,
            )
            # container_chain is a list (outer..inner archive names); the source table column is
            # TEXT, so join it to a readable path here (empty -> None for a loose/folder file).
            container_chain_text = " / ".join(file.container_chain) or None
            frame_warnings, assertion_count = build_assertions(
                records, preset, env, store, selector=selector,
                fields=list(extracted.source_fields), container_chain=container_chain_text)
            log.info(f"{label}: {len(records)} source row(s) -> {assertion_count} assertion row(s)")
            # Required fields are guaranteed present by the match gate; the frontier still
            # records any absent OPTIONAL/expected fields for the warnings sidecar.
            frontier = reporting.frontier_report(preset, list(extracted.source_fields))
            preset_ids_by_name.setdefault(preset.name, set()).add(preset.meta.id)
            sources_by_preset.setdefault(preset.name, []).append({
                "source_file": extracted.source_file,
                "raw_source_path": extracted.source_original_path,
                "input_file_path": env.input_file_path,
                "input_file_name": env.input_file_name,
                "container_chain": file.container_chain,
                "format": selector.format,
                "table": selector.table,
                "sheet": selector.sheet,
                "source_fingerprint": extracted.source_fingerprint,
                "preset_id": preset.meta.id,
                "matched_preset": preset.name,
                "parser": f"{preset.parser.name} {preset.parser.version}",
                "source_tier": preset.source_tier,
                "record_count": len(records),
                "assertion_count": assertion_count,
                "frontier": frontier,
            })
            warnings.extend(frame_warnings)

    if unmatched:
        log.info(f"{len(unmatched)} file(s) matched no preset (set log level to DEBUG to list them)")

    # All sources are loaded: project the whole store to the flat frame ONCE, then slice it into
    # the per-mode output units. Merge is the whole frame; split partitions it by preset_id.
    # Untangle scope stays per unit (merge untangles the whole merged frame, split each preset's
    # partition separately) — ranks can therefore differ between the modes, exactly as before.
    full_frame = store.to_flat_frame(
        include_source_columns=include_source_columns,
        source_column_prefix=settings.output.source_column_prefix)
    units = _build_units(
        full_frame, sources_by_preset, preset_ids_by_name, base_name, matched, unmatched, merge)
    result = _write_outputs(
        units, store=store, output_dir=output, started_at=started_at, input_path=input_path,
        presets_path=presets_path, warnings=warnings, matched=matched, unmatched=unmatched,
        traceability_format=traceability_format, exports=exports, settings=settings,
        progress=progress, merge=merge,
    )
    log.info("Processing finished.")
    return result


# ---------------------------------------------------------------------------
# Internal writers
# ---------------------------------------------------------------------------

def _order_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Canonical columns first (in schema order), then any ``orig_<col>`` passthrough
    columns in their existing (source/first-appearance) order. Keeps the merged CSV's
    column order deterministic when frames from different presets carry different
    passthrough columns."""
    if df.empty:
        return df
    canonical = [c for c in OUTPUT_COLUMNS if c in df.columns]
    extra = [c for c in df.columns if c not in set(OUTPUT_COLUMNS)]
    return df[canonical + extra]


def _counts(sources: int, matched: int, df: pd.DataFrame) -> dict[str, int]:
    """The single source of the run-counts dict, used for every sidecar AND the ProcessResult.
    ``ranked`` counts rows the untangle stage assigned a record_rank (0 when the column is absent,
    e.g. an empty frame)."""
    ranked = int(df["record_rank"].notna().sum()) if "record_rank" in df.columns else 0
    return {"sources": sources, "matched": matched, "rows": len(df), "ranked": ranked}


@dataclass(frozen=True)
class _OutputUnit:
    """One thing the writer emits: the merged frame (merge mode) or one preset's frame (split mode).

    Kept here rather than in a shared models module because it is purely the single writer's
    internal parameterisation. ``frame`` is the concatenated, NOT-yet-untangled frame (the writer
    untangles + column-orders it, preserving per-mode untangle scope). ``base_name`` is the artefact
    stem (the run base name in merge mode, the safe preset name in split mode). ``sidecar_sources`` /
    ``sidecar_matched`` are the counts written into this unit's sidecar — merge counts the whole run
    (matched + unmatched), split counts only this preset's frames — reproduced exactly from the two
    former writers."""

    preset_name: str | None          # None in merge mode, the preset name in split mode
    base_name: str
    frame: pd.DataFrame
    sources: list[dict] = field(default_factory=list)  # mutable default — a shared [] would leak
    sidecar_sources: int = 0
    sidecar_matched: int = 0


def _build_units(
    full_frame: pd.DataFrame,
    sources_by_preset: dict[str, list[dict]],
    preset_ids_by_name: dict[str, set[str]],
    base_name: str,
    matched: list[str],
    unmatched: list[str],
    merge: bool,
) -> list[_OutputUnit]:
    """One unit for merge mode (the whole run-wide flat frame), one per preset for split mode.

    Split slices the frame by ``preset_id`` (the assertion's own preset), preserving the frame's
    row order — identical to the former per-preset concatenation. ``preset_ids_by_name`` maps a
    preset name to the id(s) that share it, so presets that happen to share a name group together
    exactly as they did when keyed by name."""
    if merge:
        all_sources = [s for sources in sources_by_preset.values() for s in sources]
        return [_OutputUnit(
            preset_name=None, base_name=base_name, frame=full_frame, sources=all_sources,
            sidecar_sources=len(matched) + len(unmatched), sidecar_matched=len(matched),
        )]
    units: list[_OutputUnit] = []
    has_preset_id = "preset_id" in full_frame.columns
    for preset_name, sources in sources_by_preset.items():
        ids = preset_ids_by_name.get(preset_name, set())
        if has_preset_id and not full_frame.empty:
            preset_df = full_frame[full_frame["preset_id"].isin(ids)].reset_index(drop=True)
        else:
            preset_df = pd.DataFrame()
        units.append(_OutputUnit(
            preset_name=preset_name, base_name=_safe_filename(preset_name), frame=preset_df,
            sources=sources, sidecar_sources=len(sources), sidecar_matched=len(sources),
        ))
    return units


def _write_sidecars(
    anchor_csv: Path, content_csv: Path | None, started_at: str, input_path: Path,
    presets_path: Path, row_counts: dict, sources: list[dict], warnings: list[str],
    traceability_format: str,
) -> tuple[Path, Path]:
    """Write the traceability + warnings sidecars. Their FILE NAMES are anchored on ``anchor_csv``
    (the nominal CSV path) so they exist even when the ``csv`` export was deselected; ``content_csv``
    is the CSV actually written (or None), recorded inside the traceability document."""
    if traceability_format == "prov":
        trace = reporting.build_traceability_prov(started_at=started_at, output_csv=content_csv, sources=sources)
    else:
        trace = reporting.build_traceability_readable(
            started_at=started_at, input_path=input_path, presets_path=presets_path,
            output_csv=content_csv, row_counts=row_counts, sources=sources, warning_count=len(warnings),
        )
    output_traceability = write_json(traceability_path_for(anchor_csv), trace)
    output_warnings = write_json(warnings_path_for(anchor_csv), reporting.build_warnings_report(warnings, sources))
    return output_traceability, output_warnings


def _run_exports(
    exports: list[str],
    dataframe: pd.DataFrame,
    *,
    anchor_csv: Path,
    output_dir: Path,
    base_name: str,
    merge: bool,
    preset_name: str | None,
    started_at: str,
    input_path: Path,
    presets_path: Path,
    row_counts: dict[str, int],
    warnings: list[str],
    matched: list[str],
    sources: list[dict],
    settings: Settings,
    connection: sqlite3.Connection | None,
    progress: ProgressFn | None,
) -> list[Path]:
    """Run the selected exporters (the ``csv`` exporter among them) for one output unit. No-op when
    nothing is selected. The frame is guaranteed non-empty by the caller (empty units are skipped).
    ``connection`` is the run-wide store's live connection (the ``sqlite`` export backs it up)."""
    if not exports:
        return []
    _emit(progress, "Writing exports")
    log.info(f"Writing export(s): {', '.join(exports)}")
    context = ExportContext(
        dataframe=dataframe, output_dir=output_dir, base_name=base_name, merge=merge,
        preset_name=preset_name, primary_csv=anchor_csv, row_counts=row_counts,
        warnings=warnings, matched=matched, sources=sources, started_at=started_at,
        input_path=input_path, presets_path=presets_path, settings=settings,
        connection=connection,
    )
    return run_exporters(exports, context)


def _rank_pairs(frame: pd.DataFrame) -> list[tuple[str, Any, Any]]:
    """The ``(row_uid, record_type, record_rank)`` triples untangle assigned this frame, coerced
    to plain python (the sqlite3 driver rejects numpy scalars) so they can be written back into the
    store. A null rank stays None; the rank is otherwise an int (the assertion column's affinity)."""
    if "row_uid" not in frame.columns:
        return []
    uids = frame["row_uid"].tolist()
    types = frame["record_type"].tolist() if "record_type" in frame.columns else [None] * len(uids)
    ranks = frame["record_rank"].tolist() if "record_rank" in frame.columns else [None] * len(uids)
    pairs: list[tuple[str, Any, Any]] = []
    for uid, rtype, rank in zip(uids, types, ranks):
        # An unranked row carries pd.NA/NaN in both columns; pd.isna handles NA/NaN/None so the
        # store keeps NULL there (str(pd.NA) would be "<NA>", violating the record_type CHECK).
        rank_value = None if pd.isna(rank) else int(rank)
        type_value = None if pd.isna(rtype) else str(rtype)
        pairs.append((str(uid), type_value, rank_value))
    return pairs


def _write_outputs(
    units: list[_OutputUnit],
    *,
    store: RunStore,
    output_dir: Path,
    started_at: str,
    input_path: Path,
    presets_path: Path,
    warnings: list[str],
    matched: list[str],
    unmatched: list[str],
    traceability_format: str,
    exports: list[str] | None,
    settings: Settings,
    progress: ProgressFn | None,
    merge: bool,
) -> ProcessResult:
    """Single writer for both modes. First untangle every non-empty unit (order columns) and write
    the derived ranks back into the store — so the persisted run DB carries them — THEN run the
    selected exporters and sidecars per unit. The ``csv`` exporter writes the primary CSV; its
    returned path populates output_csv(s), the rest populate output_extras."""
    output_dir.mkdir(parents=True, exist_ok=True)
    # No selection means the default: just the primary CSV. An explicit list is used as-is, so
    # ``csv`` is written only when the caller included it.
    export_names = list(exports) if exports is not None else [PRIMARY_CSV_EXPORT_NAME]

    # Untangle every non-empty unit up front (preserving per-mode scope), collect the ranks and
    # write them ALL back into the store before any export runs. WHY: the ``sqlite`` export backs
    # up the whole store, so the ranks must be persisted before the first exporter reads them —
    # in split mode several units otherwise each back up a partially-ranked DB.
    ranked_units: list[tuple[_OutputUnit, pd.DataFrame]] = []
    rank_pairs: list[tuple[str, Any, Any]] = []
    for unit in units:
        frame = unit.frame
        if not frame.empty:
            label = (f"Untangling {len(frame)} rows" if unit.preset_name is None
                     else f"Untangling {unit.preset_name} ({len(frame)} rows)")
            _emit(progress, label)
            frame = _order_columns(untangle(frame))
            rank_pairs.extend(_rank_pairs(frame))
        if frame.empty:
            if unit.preset_name is not None:
                log.info(f"Preset {unit.preset_name} produced no rows; skipping output file.")
            continue
        ranked_units.append((unit, frame))
    store.write_ranks(rank_pairs)

    output_csvs: list[Path] = []
    output_extras: list[Path] = []
    written_frames: list[pd.DataFrame] = []
    merge_traceability: Path | None = None
    merge_warnings: Path | None = None

    for unit, frame in ranked_units:
        # The nominal primary-CSV path anchors the sidecars regardless of whether ``csv`` runs.
        anchor_csv = output_dir / f"{unit.base_name}.csv"
        counts = _counts(unit.sidecar_sources, unit.sidecar_matched, frame)
        written = _run_exports(
            export_names, frame, anchor_csv=anchor_csv, output_dir=output_dir,
            base_name=unit.base_name, merge=merge, preset_name=unit.preset_name,
            started_at=started_at, input_path=input_path, presets_path=presets_path,
            row_counts=counts, warnings=warnings, matched=matched, sources=unit.sources,
            settings=settings, connection=store.connection, progress=progress,
        )
        # The csv exporter is the one that returns the nominal path; everything else is an extra.
        csv_path = anchor_csv if anchor_csv in written else None
        output_extras.extend(path for path in written if path != anchor_csv)
        if csv_path is not None:
            output_csvs.append(csv_path)
        # Sidecars only when something was written for this frame — otherwise there is no artefact
        # to be traceable about (e.g. the caller selected no exports at all).
        if written:
            trace, warn = _write_sidecars(
                anchor_csv, csv_path, started_at, input_path, presets_path,
                counts, unit.sources, warnings, traceability_format,
            )
            if merge:
                merge_traceability, merge_warnings = trace, warn
        written_frames.append(frame)

    # ProcessResult counts span the whole run in both modes: sources = matched + unmatched, and the
    # row/ranked totals come from every written frame (one merged frame in merge mode).
    full = pd.concat(written_frames, ignore_index=True) if written_frames else pd.DataFrame()
    global_counts = _counts(len(matched) + len(unmatched), len(matched), full)

    if merge:
        return ProcessResult(
            output_csv=output_csvs[0] if output_csvs else None,
            output_traceability=merge_traceability,
            output_warnings=merge_warnings,
            row_counts=global_counts,
            warnings=warnings,
            matched=matched,
            unmatched=unmatched,
            output_extras=output_extras,
        )
    return ProcessResult(
        output_csv=None,
        output_csvs=output_csvs,
        row_counts=global_counts,
        warnings=warnings,
        matched=matched,
        unmatched=unmatched,
        output_extras=output_extras,
    )
