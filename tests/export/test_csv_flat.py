"""Golden + contract tests for the csv_flat Exporter (model_atlas/export/csv_flat.py).

This is the NEW-ENGINE golden path. A test-side pipeline runner drives the whole
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

Covers the Verify bullets:
  * the exporter output matches the frozen new goldens byte-for-byte (machine path
    normalised, exactly as the old golden test does, plus the ids DERIVED from that path —
    see _path_derived_placeholders — so the goldens reproduce from ANY checkout location);
  * the column set/order equals docs/csv_migration.md (CANONICAL_COLUMNS) + the
    per-run orig_<col> tail;
  * one file per format: multiple presets land in the single CSV, the preset_id column
    separating their rows;
  * a folder run and a zip run of the same evidence agree on content_uid (and
    claim_digest).
"""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest

from model_atlas.core.flat_view import CANONICAL_COLUMNS, PASSTHROUGH_PREFIX, build_flat_view
from model_atlas.core.store import RunStore
from model_atlas.export.csv_flat import CsvFlatExporter, ExportContext
from model_atlas.presets.spec import PresetSpec
from model_atlas.presets.spec_loader import load_preset_specs

from tests.export._pipeline import GOLDEN_SRC as _GOLDEN_SRC
from tests.export._pipeline import rows as _rows
from tests.export._pipeline import run_relational_pipeline

_GOLDEN_NEW = Path(__file__).resolve().parents[1] / "fixtures" / "golden_relational"
_CASES = ("secondary", "primary")
# Mirrors the old golden run's operator input (process(..., concerns="subject")),
# so the reviewed old->new diff stays apples-to-apples.
_CONCERNS = "subject"


def _export_merged(store: RunStore, out_dir: Path) -> Path:
    """Export the merged flat CSV and return the single file written."""
    written = CsvFlatExporter().export(
        store, ExportContext(output_dir=out_dir, base_name="merged"))
    assert written == [out_dir / "merged.csv"]
    return written[0]


def _path_derived_placeholders(store: RunStore) -> dict[str, str]:
    """Map every id the run derived FROM the input path to a location-free placeholder.

    WHY this exists at all: ``ids.source_id`` is uuid5(input_file_path ⟂ …) — by design
    (§2c: from PATHS, never the fingerprint) — and the whole id chain hangs off it:
    ``raw_record_id`` = uuid5(source_id ⟂ line_no), ``assertion_id`` =
    uuid5(raw_record_id ⟂ preset key ⟂ output_ordinal), ``activity_id`` =
    uuid5(raw_record_id ⟂ ordinal). So running the SAME fixture from a git worktree, a
    second clone or a CI layout yields DIFFERENT uuids in ``source_record_uid`` /
    ``row_uid`` / ``activity_id``, and a golden that froze those uuids only reproduces at
    one filesystem location. Normalising the ``input_file_path`` cell alone is therefore
    not enough: the path leaks into the CSV a second time, hashed.

    HOW: each id is looked up with its own NATURAL KEY — the location-free half of what
    it was derived from (format, line_no, preset key, output_ordinal) — and mapped to a
    placeholder spelling that key out. So the golden still pins the identity relation
    (this row IS line 2's assertion for preset X), and still distinguishes every row; it
    just stops pinning the one machine-dependent rendering of it. Only these ids are
    replaced — never a blanket uuid-shaped regex, which would also blank the
    content_uid/claim_digest the golden legitimately freezes (those are content-derived
    and path-free — see test_folder_run_equals_zip_run_on_digests).

    The id is NOT changed at the source: it feeds record identity and reproducibility, so
    re-deriving it path-free would be an identity change with a real blast radius.
    """
    placeholders: dict[str, str] = {}
    for row in _rows(store, "SELECT source_id, format FROM source ORDER BY format"):
        placeholders[row["source_id"]] = f"<source_uid:{row['format']}>"
    for row in _rows(
        store,
        "SELECT r.raw_record_id, r.line_no, s.format FROM raw_record r "
        "JOIN source s ON s.source_id = r.source_id ORDER BY s.format, r.line_no",
    ):
        placeholders[row["raw_record_id"]] = (
            f"<record_uid:{row['format']}/{row['line_no']}>")
    for row in _rows(
        store,
        "SELECT a.assertion_id, a.output_ordinal, p.key, r.line_no, s.format "
        "FROM assertion a "
        "JOIN raw_record r ON r.raw_record_id = a.raw_record_id "
        "JOIN source s ON s.source_id = r.source_id "
        "JOIN preset p ON p.preset_id = a.preset_id "
        "ORDER BY s.format, r.line_no, p.key, a.output_ordinal",
    ):
        placeholders[row["assertion_id"]] = (
            f"<row_uid:{row['format']}/{row['line_no']}/{row['key']}"
            f"/{row['output_ordinal']}>")
    # activity is a bonus grouping table (no ordinal column of its own), so an activity is
    # keyed by its EARLIEST linked record plus a rank over its content — both location-free.
    # The golden fixtures currently produce none; covering it keeps the invariant ("no
    # path-derived id survives into the golden") true the day a preset does emit one.
    for rank, row in enumerate(_rows(
        store,
        "SELECT ac.activity_id, MIN(s.format) AS format, MIN(r.line_no) AS line_no "
        "FROM activity ac "
        "JOIN activity_record ar ON ar.activity_id = ac.activity_id "
        "JOIN raw_record r ON r.raw_record_id = ar.raw_record_id "
        "JOIN source s ON s.source_id = r.source_id "
        "GROUP BY ac.activity_id ORDER BY format, line_no, ac.kind, ac.context_json",
    )):
        placeholders[row["activity_id"]] = (
            f"<activity_uid:{row['format']}/{row['line_no']}/{rank}>")
    return placeholders


def _normalise_machine_path(
    csv_bytes: bytes, input_path: Path, placeholders: dict[str, str],
) -> bytes:
    """Replace everything the machine's filesystem layout leaked into the CSV.

    §7.1: input_file_path is the full filesystem path of the opened artefact — the one
    machine-specific CELL — so it is normalised out before freezing/comparing, exactly as
    the old golden test does. raw_source_path/data_source now carry the in-artefact path
    (portable), so no other column carries the path VERBATIM — but the ids derived from it
    carry it hashed, hence ``placeholders`` (see _path_derived_placeholders).
    """
    machine_path = str(input_path.resolve()).encode("utf-8")
    assert machine_path in csv_bytes, "expected the resolved input path to appear verbatim"
    normalised = csv_bytes.replace(machine_path, b"<input_file_path>")
    for derived_id, placeholder in placeholders.items():
        normalised = normalised.replace(
            derived_id.encode("utf-8"), placeholder.encode("utf-8"))
    return normalised


# --- byte-parity with the frozen new goldens ---------------------------------

@pytest.mark.parametrize("case", _CASES)
def test_flat_csv_matches_new_golden(case: str, tmp_path: Path) -> None:
    spec = load_preset_specs(_GOLDEN_SRC / f"{case}_cached.preset.yaml")[0]
    input_path = _GOLDEN_SRC / "cached.csv"
    with RunStore() as store:
        run_relational_pipeline(store, input_path, [spec], concerns=_CONCERNS)
        produced = _export_merged(store, tmp_path / "out")
        # read INSIDE the with-block: the placeholders come from the store's own rows.
        placeholders = _path_derived_placeholders(store)
    actual = _normalise_machine_path(produced.read_bytes(), input_path, placeholders)

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
            store, _GOLDEN_SRC / "cached.csv", [spec], concerns=_CONCERNS)
        projected = build_flat_view(store.connection)
    for column in ("spatial_referential", "locates", "spatial_method", "temporal_method"):
        assert column in projected.columns
    first = projected.iloc[0]
    # A GNSS fix: WGS84 referential, a passthrough spatial method, a tz-localise temporal
    # method, self-location — each concrete, so encodings/derivations stay distinguishable.
    assert first["spatial_referential"] == "epsg:4326"
    assert first["locates"] == "self"
    assert first["spatial_method"].startswith("passthrough_wgs84/")
    assert first["temporal_method"].startswith("tz_localize/")


# --- one file per format: every preset's rows in the single CSV ---------------

def test_multiple_presets_land_in_one_file(tmp_path: Path) -> None:
    specs = [
        load_preset_specs(_GOLDEN_SRC / f"{case}_cached.preset.yaml")[0]
        for case in _CASES
    ]
    with RunStore() as store:
        # One evidence file, BOTH presets applied (decision 27): two assertion sets sharing
        # the raw_records. One file per format now — both sets go in the single CSV.
        run_relational_pipeline(
            store, _GOLDEN_SRC / "cached.csv", specs, concerns=_CONCERNS)
        written = CsvFlatExporter().export(
            store, ExportContext(output_dir=tmp_path, base_name="case"))

    assert written == [tmp_path / "case.csv"]
    body = written[0].read_bytes().decode("utf-8-sig").splitlines()[1:]
    # Both presets' rows (2 records -> 2 assertions each) share the one file...
    assert len(body) == 4
    # ...and the preset_id column separates them, so a caller can still split by preset.
    for preset_key in ("golden.secondary.cached", "golden.primary.cached"):
        assert sum(f'"{preset_key}"' in line for line in body) == 2


# --- folder == zip -> equal content_uid / claim_digest -----------------------

def _digest_pairs(input_path: Path, spec: PresetSpec) -> set[tuple[str, str]]:
    with RunStore() as store:
        run_relational_pipeline(store, input_path, [spec], concerns=_CONCERNS)
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
