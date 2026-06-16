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
from model_atlas.export import traceability_path_for, warnings_path_for, write_csv, write_json
from model_atlas.presets.matcher import match_preset
from model_atlas.presets.spec_loader import load_preset_specs
from model_atlas.sources import discover_elements, get_adapter
from model_atlas.transforms.assemble import BuildEnv, build_rows, to_records
from model_atlas.transforms.rank import untangle

log = logging.getLogger(__name__)

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
    traceability_format: str = "readable",
    merge: bool = True,
) -> ProcessResult:
    """Run the full pipeline.

    Args:
        input_path:           Source file or folder (CSV, Excel, SQLite).
        presets_path:         Preset YAML file or folder (scanned recursively).
        output:               In merge mode: path for the merged CSV.
                              In split mode: folder that receives one CSV per preset.
        traceability_format:  "readable" (default) or "prov" (W3C PROV-JSON).
        merge:                True → one merged output CSV (default).
                              False → one CSV per matched preset inside *output*.
    """
    started_at = reporting.now_iso()
    presets = load_preset_specs(presets_path)
    elements = discover_elements(input_path)
    log.info("Discovered %d source element(s); %d preset(s) loaded", len(elements), len(presets))

    # frames_by_preset and sources_by_preset preserve insertion order so per-preset
    # CSVs are written in the order presets were first matched.
    frames_by_preset: dict[str, list[pd.DataFrame]] = {}
    sources_by_preset: dict[str, list[dict]] = {}
    warnings: list[str] = []
    matched: list[str] = []
    unmatched: list[str] = []

    for element in elements:
        match = match_preset(element, presets)
        if match is None:
            log.info("No preset matched %s", element.source_file)
            unmatched.append(element.source_file)
            continue
        preset, _selector = match
        matched.append(f"{element.source_file} -> {preset.name}")
        extracted = get_adapter(element).extract(element, preset)
        records = to_records(extracted.dataframe)
        env = BuildEnv(
            acquisition_path=None,
            source_file_path=extracted.source_original_path,
            input_file=extracted.source_file,
            source_tier=preset.source_tier,
        )
        frame, frame_warnings = build_rows(records, preset, env, columns=list(extracted.source_columns))
        log.info("%s: %d source row(s) -> %d assertion row(s)", element.source_file, len(records), len(frame))
        frames_by_preset.setdefault(preset.name, []).append(frame)
        sources_by_preset.setdefault(preset.name, []).append({
            "source_file": extracted.source_file,
            "source_file_path": extracted.source_original_path,
            "matched_preset": preset.name,
            "parser": f"{preset.parser.name} {preset.parser.version}",
            "source_tier": preset.source_tier,
            "record_count": len(records),
            "assertion_count": len(frame),
            "frontier": reporting.frontier_report(preset, list(extracted.source_columns)),
        })
        warnings.extend(frame_warnings)

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
        merged = untangle(merged)
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
            preset_df = untangle(preset_df)
        if preset_df.empty:
            log.info("Preset %s produced no rows; skipping output file.", preset_name)
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
