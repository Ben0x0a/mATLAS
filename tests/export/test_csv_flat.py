"""Golden + contract tests for the csv_flat Exporter (model_atlas/export/csv_flat.py).

This is the NEW-ENGINE golden path (T50). A test-side pipeline runner drives the whole
brick chain over the SAME committed fixtures the old golden test uses
(tests/fixtures/golden/): ingest_input (L0) -> assemble_source (L1) ->
integrate_spatial + integrate temporal (L2) -> normalise_entities -> untangle.analyse
(L3) -> CsvFlatExporter (projection). The expected CSVs are frozen under a NEW directory
(tests/fixtures/golden_relational/) so the OLD goldens are never touched — they still
gate the old engine until the switch task.

Byte-parity with the pre-refactor CSV is retired (row_uid/source_record_uid values change
by design — docs/csv_migration.md); the parity that remains is folder==zip -> equal
content_uid / claim_digest, exercised here directly.

To intentionally update the new goldens after a reviewed change:
    MATLAS_REGEN_GOLDEN=1 .venv/bin/python -m pytest tests/export/test_csv_flat.py

Covers the T50 Verify bullets:
  * the exporter output matches the frozen new goldens byte-for-byte (machine path
    normalised, exactly as the old golden test does);
  * the column set/order equals docs/csv_migration.md (CANONICAL_COLUMNS) + the
    per-run orig_<col> tail;
  * split mode partitions by preset_id with identical per-partition columns;
  * a folder run and a zip run of the same evidence agree on content_uid (and
    claim_digest).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.projection import CANONICAL_COLUMNS, PASSTHROUGH_PREFIX, project
from model_atlas.core.store import RunStore
from model_atlas.export.csv_flat import CsvFlatExporter, ExportContext
from model_atlas.ingest.assemble import assemble_source
from model_atlas.ingest.readers.base import RawRecord
from model_atlas.ingest.writer import ingest_input
from model_atlas.integrate.entity import normalise_entities
from model_atlas.integrate.spatial import integrate_spatial
from model_atlas.integrate.temporal import (
    TemporalExpressionBundle,
    integrate_expression,
    standard_methods,
)
from model_atlas.analyze.untangle import analyse
from model_atlas.presets.spec import PresetSpec
from model_atlas.presets.spec_loader import load_preset_specs

_GOLDEN_SRC = Path(__file__).resolve().parents[1] / "fixtures" / "golden"
_GOLDEN_NEW = Path(__file__).resolve().parents[1] / "fixtures" / "golden_relational"
_CASES = ("secondary", "primary")
# Mirrors the old golden run's operator input (process(..., linked_entity="subject")),
# so the reviewed old->new diff stays apples-to-apples.
_LINKED_ENTITY = "subject"


# --- the test-side pipeline runner -------------------------------------------
# WHY a test-side runner: the app-layer pipeline that wires these bricks together is a
# later task (T60); until it exists the new engine's chain is driven here, following the
# established pattern of tests/ingest/test_assemble.py (drive the writers directly).

def _preset_row(spec: PresetSpec) -> V.Preset:
    """The store's ``preset`` row for a loaded spec.

    ``preset_id`` derives from (key ⟂ version) exactly as ``assemble_source`` re-derives
    it, so the assertion FK matches by construction; ``name`` carries the spec's display
    title (the human string the old flat model showed as ``preset_name``); the
    ``definition_digest`` is the YAML bytes' sha256 (stable + change-detecting for D40).
    """
    return V.Preset(
        preset_id=ids.preset_id(spec.meta.id, spec.meta.version),
        key=spec.meta.id,
        name=spec.meta.title,
        version=spec.meta.version,
        definition_digest=hashlib.sha256(spec.path.read_bytes()).hexdigest(),
    )


def _rows(store: RunStore, sql: str, *args: object) -> list[dict]:
    cursor = store.connection.execute(sql, args)
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _source_units(specs: list[PresetSpec]) -> dict[str, str]:
    """The part -> declared source unit map (e.g. horizontal_speed_kmh -> m/s), gathered
    from every spec's field ``unit`` attributes — the reference data L2's UnitConvert needs
    to read a verbatim metric value into its canonical unit."""
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


def _integrate_temporal_for_preset(store: RunStore, spec: PresetSpec) -> None:
    """Integrate the not-yet-integrated temporal expressions this spec produced.

    HOW: re-read each temporal_expression reachable from this preset's assertions plus its
    verbatim component rows, wrap them as a method bundle, and run the standard method set
    configured with the spec's own datetime format. WHY per preset (not one whole-store
    pass): the parsing format/zone live in the preset, not the store, so each preset's
    expressions must be integrated with THAT preset's decode configuration.
    """
    methods = standard_methods(datetime_format=_datetime_format(spec))
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


def run_relational_pipeline(
    store: RunStore,
    input_path: Path,
    specs: list[PresetSpec],
    *,
    case_number: str = "C1",
    linked_entity: str | None = None,
) -> None:
    """Drive the full new-engine chain over ``input_path`` into ``store``.

    Ingest (L0) writes case/run/preset/source/raw_record; each matching preset is
    assembled onto every source (L1); spatial + temporal integrations are written (L2);
    entities are normalised (MUST precede untangle so a converged identifier is visible);
    untangle writes the analysis verdicts (L3). ``data_source`` resolved by assemble is
    set back on the source row here (decision 24 — the L0 owner's job, which this runner
    stands in for), so ``raw_source_path`` reports the in-artefact path, not the opened
    file. Digests are unaffected (data_source is source-grain, outside both digests), so
    folder==zip parity holds.
    """
    preset_rows = tuple(_preset_row(spec) for spec in specs)
    ingest_input(input_path, store, case_number=case_number, presets=preset_rows)

    # activity ids are record-scoped (preset-free), so one ordinal registry is shared
    # across every assemble call in the run (see assemble_source's caller contract).
    activity_ordinals: dict[str, int] = {}
    for source in _rows(store, "SELECT source_id, fingerprint_sha256, format FROM source"):
        records = [
            RawRecord(line_no=row["line_no"], fields=json.loads(row["raw_json"]))
            for row in _rows(
                store,
                "SELECT line_no, raw_json FROM raw_record WHERE source_id = ? ORDER BY line_no",
                source["source_id"],
            )
        ]
        for spec in specs:
            # A spec applies to a source when its selector format matches (the golden
            # presets select cached.csv by name; the single CSV source matches both).
            if spec.input_selectors[0].format != source["format"]:
                continue
            report = assemble_source(
                records, spec, store,
                source_id=source["source_id"],
                fingerprint_sha256=source["fingerprint_sha256"],
                linked_entity=linked_entity,
                activity_ordinals=activity_ordinals,
            )
            if report.data_source is not None:
                store.connection.execute(
                    "UPDATE source SET data_source = ? WHERE source_id = ?",
                    (report.data_source, source["source_id"]),
                )
            _integrate_temporal_for_preset(store, spec)

    integrate_spatial(store, source_units=_source_units(specs))
    normalise_entities(store)
    analyse(store)


def _export_merged(store: RunStore, out_dir: Path) -> Path:
    """Export the merged flat CSV and return the single file written."""
    written = CsvFlatExporter().export(
        store, ExportContext(output_dir=out_dir, base_name="merged", merge=True))
    assert written == [out_dir / "merged.csv"]
    return written[0]


def _normalise_machine_path(csv_bytes: bytes, input_path: Path) -> bytes:
    """Replace the machine-specific input_file_path with a portable placeholder.

    §7.1: input_file_path is the full filesystem path of the opened artefact — the one
    machine-specific cell — so it is normalised out before freezing/comparing, exactly as
    the old golden test does. raw_source_path/data_source now carry the in-artefact path
    (portable), so only this column needs normalising.
    """
    machine_path = str(input_path.resolve()).encode("utf-8")
    assert machine_path in csv_bytes, "expected the resolved input path to appear verbatim"
    return csv_bytes.replace(machine_path, b"<input_file_path>")


# --- byte-parity with the frozen new goldens ---------------------------------

@pytest.mark.parametrize("case", _CASES)
def test_flat_csv_matches_new_golden(case: str, tmp_path: Path) -> None:
    spec = load_preset_specs(_GOLDEN_SRC / f"{case}_cached.preset.yaml")[0]
    input_path = _GOLDEN_SRC / "cached.csv"
    with RunStore() as store:
        run_relational_pipeline(store, input_path, [spec], linked_entity=_LINKED_ENTITY)
        produced = _export_merged(store, tmp_path / "out")
    actual = _normalise_machine_path(produced.read_bytes(), input_path)

    golden = _GOLDEN_NEW / f"expected_{case}.csv"
    if os.environ.get("MATLAS_REGEN_GOLDEN"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_bytes(actual)
        pytest.skip(f"regenerated {golden.name}")

    assert actual == golden.read_bytes(), (
        f"flat CSV drifted from {golden.name} for the {case!r} case.\n"
        f"If this change is intentional, regenerate with MATLAS_REGEN_GOLDEN=1."
    )


def test_new_goldens_utf8_sig_bom() -> None:
    """Every frozen new golden is written utf-8-sig (the house file-output encoding)."""
    for case in _CASES:
        data = (_GOLDEN_NEW / f"expected_{case}.csv").read_bytes()
        assert data[:3] == b"\xef\xbb\xbf", f"expected_{case}.csv is missing the utf-8 BOM"


# --- the column contract (docs/csv_migration.md via CANONICAL_COLUMNS) --------

def _header(csv_bytes: bytes) -> list[str]:
    text = csv_bytes.decode("utf-8-sig")
    return [cell.strip('"') for cell in text.splitlines()[0].split(";")]


@pytest.mark.parametrize("case", _CASES)
def test_column_order_equals_contract(case: str) -> None:
    header = _header((_GOLDEN_NEW / f"expected_{case}.csv").read_bytes())
    canonical, tail = header[: len(CANONICAL_COLUMNS)], header[len(CANONICAL_COLUMNS):]
    # The canonical block is the exact docs/csv_migration.md order (no drift, no gap).
    assert tuple(canonical) == CANONICAL_COLUMNS
    # The tail is the per-run orig_<col> passthrough (kept — a hard forensic constraint).
    assert tail and all(name.startswith(PASSTHROUGH_PREFIX) for name in tail)


def test_mandatory_r6_columns_present_and_distinguishing() -> None:
    """R6: the four always-present columns exist, and a GPS fix's referential/methods are
    concrete (never blank) — so a fix, a geocoder centroid and a cell lookup can differ."""
    spec = load_preset_specs(_GOLDEN_SRC / "secondary_cached.preset.yaml")[0]
    with RunStore() as store:
        run_relational_pipeline(
            store, _GOLDEN_SRC / "cached.csv", [spec], linked_entity=_LINKED_ENTITY)
        projected = project(store.connection)
    for column in ("spatial_referential", "locates", "spatial_method", "temporal_method"):
        assert column in projected.columns
    first = projected.iloc[0]
    # A GNSS fix: WGS84 referential, a passthrough spatial method, a tz-localise temporal
    # method, self-location — each concrete, so encodings/derivations stay distinguishable.
    assert first["spatial_referential"] == "epsg:4326"
    assert first["locates"] == "self"
    assert first["spatial_method"].startswith("passthrough_wgs84/")
    assert first["temporal_method"].startswith("tz_localize/")


# --- split mode: partition by preset, identical per-partition columns ---------

def test_split_mode_partitions_by_preset(tmp_path: Path) -> None:
    specs = [
        load_preset_specs(_GOLDEN_SRC / f"{case}_cached.preset.yaml")[0]
        for case in _CASES
    ]
    with RunStore() as store:
        # One evidence file, BOTH presets applied (decision 27): two assertion sets sharing
        # the raw_records — the case split mode exists to separate.
        run_relational_pipeline(
            store, _GOLDEN_SRC / "cached.csv", specs, linked_entity=_LINKED_ENTITY)
        merged = _export_merged(store, tmp_path / "merged")
        split = CsvFlatExporter().export(
            store, ExportContext(output_dir=tmp_path / "split", merge=False))

    # One file per preset, named after its key.
    assert {path.name for path in split} == {
        "golden.secondary.cached.csv", "golden.primary.cached.csv"}

    merged_header = _header(merged.read_bytes())
    for path in split:
        part_header = _header(path.read_bytes())
        # Every partition carries the identical column set/order of the merged file — split
        # slices the ONE projection, it is not a second writer path.
        assert part_header == merged_header
        # A partition holds exactly its preset's rows (2 records -> 2 assertions each).
        body = path.read_bytes().decode("utf-8-sig").splitlines()[1:]
        assert len(body) == 2
        preset_key = path.stem
        assert all(f'"{preset_key}"' in line for line in body)


# --- folder == zip -> equal content_uid / claim_digest -----------------------

def _digest_pairs(input_path: Path, spec: PresetSpec) -> set[tuple[str, str]]:
    with RunStore() as store:
        run_relational_pipeline(store, input_path, [spec], linked_entity=_LINKED_ENTITY)
        return {
            (row["content_hash"], row["claim_digest"])
            for row in _rows(store, "SELECT content_hash, claim_digest FROM assertion")
        }


def test_folder_run_equals_zip_run_on_digests(tmp_path: Path) -> None:
    spec = load_preset_specs(_GOLDEN_SRC / "secondary_cached.preset.yaml")[0]
    csv_bytes = (_GOLDEN_SRC / "cached.csv").read_bytes()

    folder = tmp_path / "tree"
    folder.mkdir()
    (folder / "cached.csv").write_bytes(csv_bytes)

    zip_path = tmp_path / "tree.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("cached.csv", csv_bytes)
    zip_path.write_bytes(buffer.getvalue())

    folder_digests = _digest_pairs(folder, spec)
    zip_digests = _digest_pairs(zip_path, spec)
    # Two disclosures (a folder and a zip), but no path reaches a digest — so both the
    # content_uid and the claim_digest of every assertion agree (R5 same-evidence finding).
    assert len(folder_digests) == 2
    assert folder_digests == zip_digests
