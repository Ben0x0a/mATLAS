"""Declarative processing pipeline.

Defines:    process — discover sources, match declarative presets, extract via the
            adapter registry, assemble assertion rows, untangle, write CSV(s) plus
            traceability (readable or PROV-JSON) and warnings+frontier sidecars.
            merge=True  → one merged CSV at the given output path.
            merge=False → one CSV per matched preset written into the output folder.
Used by:    the CLI and tests.
Depends on: sources (discovery + adapter registry), presets.spec_loader, matcher,
            transforms.assemble + rank, reporting, export.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from model_atlas import reporting
from model_atlas.config import get_settings
from model_atlas.export import traceability_path_for, warnings_path_for, write_csv, write_json
from model_atlas.model.families import OUTPUT_COLUMNS
from model_atlas.presets.matcher import detect_file_format, match_file
from model_atlas.presets.spec import InputSelector, PresetSpec
from model_atlas.presets.spec_loader import load_preset_specs
from model_atlas.sources import SingleSourceExtractor, SourceFile, ZipContainer, discover
from model_atlas.sources.container import FilesystemContainer
from model_atlas.sources.readers import get_reader
from model_atlas.transforms.assemble import BuildEnv, build_rows, to_records
from model_atlas.transforms.rank import untangle

log = logging.getLogger(__name__)


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


def _input_detected_format(path: Path) -> str | None:
    """Magic-detect the format of an on-disk input file (to tell archive from leaf file)."""
    import zipfile

    from model_atlas.sources.format_detect import detect_format

    with path.open("rb") as fh:
        head = fh.read(512)

    def peek_zip() -> list[str]:
        try:
            with zipfile.ZipFile(path) as zf:
                return zf.namelist()
        except (OSError, zipfile.BadZipFile):
            return []

    return detect_format(head, peek_zip, path.suffix)


def _peek(file: SourceFile, selector: InputSelector) -> "set[str] | None":
    return get_reader(selector.format).peek_columns(file, selector)


def _resolve_force_selector(
    file: SourceFile, preset: PresetSpec, detected: str | None
) -> InputSelector:
    """Force mode: ignore location, but verify ``format`` and resolve the in-file sub-unit.

    Picks the first selector whose format matches the file (hard error on mismatch), then
    for excel/sqlite falls back to a structurally-best sheet/table when the declared one is
    absent (force-mode only — auto matching stays precise)."""
    import dataclasses

    from model_atlas.presets.matcher import _structural_score

    selectors = [s for s in preset.input_selectors if s.format == detected]
    if not selectors:
        raise ValueError(
            f"force-preset format mismatch: {file.logical_path} is {detected}, "
            f"preset {preset.name!r} declares {[s.format for s in preset.input_selectors]}"
        )
    selector = selectors[0]
    reader = get_reader(selector.format)
    if selector.format == "csv" or selector.sql:
        return selector  # csv: whole file; sql: run as written (a missing table just errors)

    declared = selector.sheet if selector.format == "excel" else selector.table
    subtables = reader.list_subtables(file)
    if declared in subtables:
        return selector

    # Choose the qualifying sub-unit (all referenced columns present) with the best score.
    from model_atlas.reporting import _column_refs
    from model_atlas.transforms.assemble import make_resolver

    refs = list(preset.expected_columns) or _column_refs(preset)
    best: tuple[int, str] | None = None
    for sub in subtables:
        probe = dataclasses.replace(
            selector, **({"sheet": sub} if selector.format == "excel" else {"table": sub})
        )
        columns = reader.peek_columns(file, probe)
        if columns is None:
            continue
        resolve = make_resolver(sorted(columns))
        if all(resolve(ref) is not None for ref in refs):
            score = _structural_score(preset, columns)
            if best is None or score > best[0]:
                best = (score, sub)
    if best is None:
        raise ValueError(
            f"force-preset {preset.name!r}: no {selector.format} sub-unit in "
            f"{file.logical_path} satisfies the mapped columns"
        )
    return dataclasses.replace(
        selector, **({"sheet": best[1]} if selector.format == "excel" else {"table": best[1]})
    )

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


def process(
    input_path: Path,
    presets_path: Path,
    output: Path,
    *,
    linked_entity: str,
    traceability_format: str = "readable",
    merge: bool = True,
    entity: str | None = None,
    include_source_columns: bool = True,
    root_prefix_depth: int | None = None,
    max_container_depth: int | None = None,
    local_zone: str | None = None,
) -> ProcessResult:
    """Run the full pipeline.

    Args:
        input_path:           Source file or folder (CSV, Excel, SQLite).
        presets_path:         Preset YAML file or folder (scanned recursively).
        output:               In merge mode: path for the merged CSV.
                              In split mode: folder that receives one CSV per preset.
        linked_entity:        Required. The entity linked to every output row (e.g. the
                              case subject). When supplied it OVERRIDES any linked_entity
                              a preset maps; a preset value is the default only when no
                              caller value reaches here. Declared as a required keyword
                              so the package API makes the obligation explicit.
        traceability_format:  "readable" (default) or "prov" (W3C PROV-JSON).
        merge:                True → one merged output CSV (default).
                              False → one CSV per matched preset inside *output*.
        entity:               Optional entity. When supplied it OVERRIDES any entity the
                              preset maps; the preset value is the default used otherwise.
        include_source_columns: True (default) → append every source column verbatim as
                              an ``orig_<col>`` column after the canonical schema. False →
                              canonical columns only.
    """
    # Per-run args win; otherwise fall back to matlas_config.toml (or built-in defaults).
    settings = get_settings()
    if root_prefix_depth is None:
        root_prefix_depth = settings.discovery.root_prefix_depth
    if max_container_depth is None:
        max_container_depth = settings.discovery.max_container_depth
    if local_zone is None:
        local_zone = settings.timezone.local_zone

    started_at = reporting.now_iso()
    presets = load_preset_specs(presets_path)
    files = discover(input_path, max_container_depth=max_container_depth)
    log.info(f"Discovered {len(files)} source file(s); {len(presets)} preset(s) loaded")

    # Force-preset mode: the input is a single ordinary file (its detected format is NOT
    # an archive) and the presets argument is one YAML with exactly one preset. Then the
    # preset is applied to that one file WITHOUT path/name matching — but its `format` is
    # still verified (hard error on mismatch) and the in-file sub-unit still resolved. A
    # single zip/folder is a Container -> normal matching.
    input_is_file = Path(input_path).is_file()
    input_is_archive = input_is_file and _input_detected_format(Path(input_path)) == "archive"
    force_preset = (
        input_is_file
        and not input_is_archive
        and Path(presets_path).is_file()
        and len(presets) == 1
    )
    if force_preset:
        log.info(f"Force-preset mode: applying {presets[0].name!r} (location matching bypassed)")

    extractor = SingleSourceExtractor()
    # frames_by_preset and sources_by_preset preserve insertion order so per-preset
    # CSVs are written in the order presets were first matched.
    frames_by_preset: dict[str, list[pd.DataFrame]] = {}
    sources_by_preset: dict[str, list[dict]] = {}
    warnings: list[str] = []
    matched: list[str] = []
    unmatched: list[str] = []

    for file in files:
        label = str(file.full_logical_path)
        if force_preset:
            preset = presets[0]
            selector = _resolve_force_selector(file, preset, detect_file_format(file))
        else:
            match = match_file(file, presets, root_prefix_depth=root_prefix_depth, peek=_peek)
            if match is None:
                log.debug(f"No preset matched {label}")
                unmatched.append(label)
                continue
            preset, selector = match
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
        frame, frame_warnings = build_rows(
            records, preset, env, selector=selector, columns=list(extracted.source_columns),
            include_source_columns=include_source_columns)
        log.info(f"{label}: {len(records)} source row(s) -> {len(frame)} assertion row(s)")
        frames_by_preset.setdefault(preset.name, []).append(frame)
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
            "assertion_count": len(frame),
            "frontier": reporting.frontier_report(preset, list(extracted.source_columns)),
        })
        warnings.extend(frame_warnings)

    if unmatched:
        log.info(f"{len(unmatched)} file(s) matched no preset (set log level to DEBUG to list them)")

    if merge:
        return _write_merged(
            output, started_at, input_path, presets_path,
            frames_by_preset, sources_by_preset,
            warnings, matched, unmatched, traceability_format,
        )
    return _write_split(
        output, started_at, input_path, presets_path,
        frames_by_preset, sources_by_preset,
        warnings, matched, unmatched, traceability_format,
    )


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


def _row_counts(frames_by_preset: dict, unmatched: list, matched: list, df: pd.DataFrame) -> dict[str, int]:
    ranked = int(df["record_rank"].notna().sum()) if "record_rank" in df.columns else 0
    return {
        "sources": len(matched) + len(unmatched),
        "matched": len(matched),
        "rows": len(df),
        "ranked": ranked,
    }


def _write_sidecars(
    csv_path: Path, started_at: str, input_path: Path, presets_path: Path,
    row_counts: dict, sources: list[dict], warnings: list[str], traceability_format: str,
) -> tuple[Path, Path]:
    if traceability_format == "prov":
        trace = reporting.build_traceability_prov(started_at=started_at, output_csv=csv_path, sources=sources)
    else:
        trace = reporting.build_traceability_readable(
            started_at=started_at, input_path=input_path, presets_path=presets_path,
            output_csv=csv_path, row_counts=row_counts, sources=sources, warning_count=len(warnings),
        )
    output_traceability = write_json(traceability_path_for(csv_path), trace)
    output_warnings = write_json(warnings_path_for(csv_path), reporting.build_warnings_report(warnings, sources))
    return output_traceability, output_warnings


def _write_merged(
    output_csv: Path,
    started_at: str,
    input_path: Path,
    presets_path: Path,
    frames_by_preset: dict[str, list[pd.DataFrame]],
    sources_by_preset: dict[str, list[dict]],
    warnings: list[str],
    matched: list[str],
    unmatched: list[str],
    traceability_format: str,
) -> ProcessResult:
    all_frames = [f for frames in frames_by_preset.values() for f in frames]
    all_sources = [s for sources in sources_by_preset.values() for s in sources]
    merged = pd.concat(all_frames, ignore_index=True) if all_frames else pd.DataFrame()
    if not merged.empty:
        merged = _order_columns(untangle(merged))
    written = write_csv(merged, output_csv) if not merged.empty else None
    counts = _row_counts(frames_by_preset, unmatched, matched, merged)

    output_traceability: Path | None = None
    output_warnings: Path | None = None
    if written is not None:
        output_traceability, output_warnings = _write_sidecars(
            written, started_at, input_path, presets_path,
            counts, all_sources, warnings, traceability_format,
        )

    return ProcessResult(
        output_csv=written,
        output_traceability=output_traceability,
        output_warnings=output_warnings,
        row_counts=counts,
        warnings=warnings,
        matched=matched,
        unmatched=unmatched,
    )


def _write_split(
    output_folder: Path,
    started_at: str,
    input_path: Path,
    presets_path: Path,
    frames_by_preset: dict[str, list[pd.DataFrame]],
    sources_by_preset: dict[str, list[dict]],
    warnings: list[str],
    matched: list[str],
    unmatched: list[str],
    traceability_format: str,
) -> ProcessResult:
    output_folder.mkdir(parents=True, exist_ok=True)
    output_csvs: list[Path] = []
    total_rows = 0
    total_ranked = 0

    for preset_name, frames in frames_by_preset.items():
        sources = sources_by_preset.get(preset_name, [])
        preset_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if not preset_df.empty:
            preset_df = _order_columns(untangle(preset_df))
        if preset_df.empty:
            log.info(f"Preset {preset_name} produced no rows; skipping output file.")
            continue
        csv_path = output_folder / f"{_safe_filename(preset_name)}.csv"
        written = write_csv(preset_df, csv_path)
        output_csvs.append(written)
        ranked = int(preset_df["record_rank"].notna().sum()) if "record_rank" in preset_df.columns else 0
        total_rows += len(preset_df)
        total_ranked += ranked
        preset_counts = {
            "sources": len(frames),
            "matched": len(frames),
            "rows": len(preset_df),
            "ranked": ranked,
        }
        _write_sidecars(
            written, started_at, input_path, presets_path,
            preset_counts, sources, warnings, traceability_format,
        )

    global_counts = {
        "sources": len(matched) + len(unmatched),
        "matched": len(matched),
        "rows": total_rows,
        "ranked": total_ranked,
    }
    return ProcessResult(
        output_csv=None,
        output_csvs=output_csvs,
        row_counts=global_counts,
        warnings=warnings,
        matched=matched,
        unmatched=unmatched,
    )
