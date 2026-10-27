"""Shared test-side store builders for tests/export/.

Defines:    ``GOLDEN_SRC`` (the committed golden fixture directory), ``rows`` (a
            dict-row SELECT helper), ``run_relational_pipeline`` (drives the full
            new-engine brick chain over a golden fixture into a ``RunStore`` — the
            same runner ``test_csv_flat.py`` used before this extraction) and
            ``build_manual_store`` (a small, hand-authored two-observation store, for
            exercising GeoVisualizer's interval-bound expansion and IMEI extraction
            under full control, without going through a preset).
Used by:    tests/export/test_csv_flat.py, test_kml.py, test_geojson.py,
            test_xlsx.py, test_geovisualizer.py, test_sqlite_db.py.
Depends on: model_atlas.core (ids, store, vocabularies), model_atlas.ingest,
            model_atlas.integrate, model_atlas.analyse, model_atlas.presets.

Not a test module itself (leading underscore, per the ``exporters.registry`` plugin
convention of skipping ``_``-prefixed files) — pytest does not collect it.

Why factor this out
----------------------
``test_csv_flat.py`` already built a test-side pipeline runner (the app-layer
pipeline that wires these bricks together came later). Five more export bricks need
the SAME runner over the SAME golden fixtures, so the
runner is factored out here rather than duplicated five times; ``test_csv_flat.py`` now
imports it instead of defining it locally. This module stays inside tests/export/, so
no other test package is touched.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore
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
from model_atlas.analyse.untangle import analyse
from model_atlas.presets.spec import PresetSpec

GOLDEN_SRC = Path(__file__).resolve().parents[1] / "fixtures" / "golden"


# --- the test-side pipeline runner (golden-fixture path) --------------------

def _preset_row(spec: PresetSpec) -> V.Preset:
    """The store's ``preset`` row for a loaded spec.

    ``preset_id`` derives from (key ⟂ version) exactly as ``assemble_source``
    re-derives it, so the observation FK matches by construction; ``name`` carries the
    spec's display title (the human string the old flat model showed as
    ``preset_name``); the ``definition_digest`` is the YAML bytes' sha256 (stable +
    change-detecting for D40).
    """
    return V.Preset(
        preset_id=ids.preset_id(spec.meta.id, spec.meta.version),
        key=spec.meta.id,
        name=spec.meta.title,
        version=spec.meta.version,
        definition_digest=hashlib.sha256(spec.path.read_bytes()).hexdigest(),
    )


def rows(store: RunStore, sql: str, *args: object) -> list[dict]:
    cursor = store.connection.execute(sql, args)
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _source_units(specs: list[PresetSpec]) -> dict[str, str]:
    """The part -> declared source unit map (e.g. horizontal_speed_kmh -> m/s), gathered
    from every spec's field ``unit`` attributes — the reference data L2's UnitConvert
    needs to read a verbatim metric value into its canonical unit."""
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


def _integrate_temporal_for_preset(store: RunStore, spec: PresetSpec) -> None:
    """Integrate the not-yet-integrated temporal expressions this spec produced.

    HOW: re-read each temporal_expression reachable from this preset's observations plus
    its verbatim component rows, wrap them as a method bundle, and run the standard
    method set configured with the spec's own datetime format. WHY per preset (not one
    whole-store pass): the parsing format/zone live in the preset, not the store, so
    each preset's expressions must be integrated with THAT preset's decode
    configuration.
    """
    methods = standard_methods(datetime_format=_datetime_format(spec))
    preset_id = ids.preset_id(spec.meta.id, spec.meta.version)
    expressions = rows(
        store,
        "SELECT DISTINCT te.temporal_expression_id, te.kind, te.referential, "
        "te.temporal_source FROM temporal_expression te "
        "JOIN observation a ON a.temporal_expression_id = te.temporal_expression_id "
        "WHERE a.preset_id = ? AND te.temporal_expression_id NOT IN "
        "(SELECT temporal_expression_id FROM timespan_integration)",
        preset_id,
    )
    for expression in expressions:
        components = rows(
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


def run_relational_pipeline(
    store: RunStore,
    input_path: Path,
    specs: list[PresetSpec],
    *,
    case_number: str = "C1",
    concerns: str | None = None,
) -> None:
    """Drive the full new-engine chain over ``input_path`` into ``store``.

    Ingest (L0) writes case/run/preset/source/raw_record; each matching preset is
    assembled onto every source (L1); spatial + temporal integrations are written
    (L2); entities are normalised (MUST precede untangle so a converged identifier is
    visible); untangle writes the analysis verdicts (L3). ``reported_data_source`` resolved by
    assemble is set back on the source row here (decision 24 — the L0 owner's job,
    which this runner stands in for), so ``raw_source_path`` reports the in-artefact
    path, not the opened file. Digests are unaffected (it is source-grain,
    outside both digests), so folder==zip parity holds.
    """
    preset_rows = tuple(_preset_row(spec) for spec in specs)
    ingest_input(input_path, store, case_number=case_number, presets=preset_rows)

    for source in rows(store, "SELECT source_file_id, fingerprint_sha256, format FROM source_file"):
        records = [
            RawRecord(line_no=row["line_no"], fields=json.loads(row["raw_json"]),
                      raw_record_id=row["raw_record_id"])
            for row in rows(
                store,
                "SELECT raw_record_id, line_no, raw_json FROM raw_record "
                "WHERE source_file_id = ? ORDER BY line_no",
                source["source_file_id"],
            )
        ]
        for spec in specs:
            # A spec applies to a source when its selector format matches (the golden
            # presets select cached.csv by name; the single CSV source matches both).
            if spec.input_selectors[0].format != source["format"]:
                continue
            report = assemble_source(
                records, spec, store,
                source_file_id=source["source_file_id"],
                fingerprint_sha256=source["fingerprint_sha256"],
                concerns=concerns,
            )
            # Mirrors the pipeline: the reported path is a SECONDARY tool's second-hand
            # claim, so a primary acquisition drops it — input_file_path is already the
            # verified location there, and an unverifiable path must not sit beside it.
            if (report.data_source is not None
                    and spec.source_tier != V.SourceTier.PRIMARY.value):
                store.connection.execute(
                    "UPDATE source_file SET reported_data_source = ? WHERE source_file_id = ?",
                    (report.data_source, source["source_file_id"]),
                )
            # The matched preset's declared tier overrides the L0 'primary' default
            # (mirrors the pipeline; source-grain, outside the digests).
            store.connection.execute(
                "UPDATE source_file SET source_tier = ? WHERE source_file_id = ?",
                (spec.source_tier, source["source_file_id"]),
            )
            _integrate_temporal_for_preset(store, spec)

    integrate_spatial(store, source_units=_source_units(specs))
    normalise_entities(store)
    analyse(store)


# --- a small, hand-authored store (no preset involved) -----------------------
# Built directly with the family dataclasses so a caller has FULL control over the
# shape (a genuine interval, a compound IMEI-bearing identifier) that the golden
# fixtures (both instant-only, plain "device" entities) cannot exercise. Every id
# below is a literal test string: the store enforces referential integrity via FK
# constraints, not any particular id SHAPE, so ids.py's uuid5 derivation is not needed
# for a store nothing else cross-references.

_CASE_ID = "case-manual-1"
_RUN_ID = "run-manual-1"
_PRESET_ID = "preset-manual-1"
_SOURCE_ID = "source-manual-1"

# A valid-Luhn 15-digit IMEI, embedded in a compound identifier exactly as the old
# GeoVisualizer exporter's docstring names ("dev_490154203237518-x") — exercises the
# separator-splitting _pick_imei port.
MANUAL_IMEI = "490154203237518"
MANUAL_IMEI_IDENTIFIER = f"dev_{MANUAL_IMEI}-x"


def _write_observation(
    store: RunStore,
    *,
    tag: str,
    identifier: str | None,
    identifier_type: V.IdentifierType,
    latitude: float,
    longitude: float,
    horizontal_accuracy_m: float,
    horizontal_speed_kmh: float | None,
    lower_us: int,
    upper_us: int,
    offset_lower: float | str | None,
    offset_upper: float | str | None,
    lower_field: str,
    upper_field: str,
    line_no: int,
) -> None:
    """Write one complete, self-contained observation: its raw_record, entity, owned
    corners (spatial + temporal, header + one component each) and their L2
    integrations. ``lower_us``/``upper_us`` equal for an instant, distinct for a
    genuine interval — the case GeoVisualizer's row-per-bound expansion exists for.
    """
    raw_record_id = f"record-{tag}"
    entity_id = f"entity-{tag}"
    spatial_expression_id = f"sx-{tag}"
    temporal_expression_id = f"tx-{tag}"
    observation_id = f"observation-{tag}"

    store.write_raw_record(V.RawRecord(
        raw_record_id=raw_record_id, source_file_id=_SOURCE_ID, line_no=line_no, raw_json="{}"))

    store.write_entity(V.Entity(
        entity_id=entity_id, identifier=identifier, raw_identifier=identifier,
        identifier_type=identifier_type, type=V.EntityType.DEVICE,
        concerns="subject", concerns_origin=V.Origin.OPERATOR_PARAM))

    store.write_spatial_expression(V.SpatialExpression(
        spatial_expression_id=spatial_expression_id,
        position_type=V.PositionType.COORDINATE,
        referential=V.SpatialReferential.EPSG_4326, spatial_source="GNSS"))
    store.write_spatial_component(V.SpatialComponent(
        spatial_component_id=f"sc-{tag}-lat", spatial_expression_id=spatial_expression_id,
        ordinal=0, part="latitude", source_field="Latitude", raw_value=str(latitude)))
    store.write_spatial_component(V.SpatialComponent(
        spatial_component_id=f"sc-{tag}-lon", spatial_expression_id=spatial_expression_id,
        ordinal=1, part="longitude", source_field="Longitude", raw_value=str(longitude)))
    store.write_spatial_integration(V.SpatialIntegration(
        spatial_integration_id=f"si-{tag}", spatial_expression_id=spatial_expression_id,
        method="passthrough_wgs84/1.0", latitude_wgs84=latitude, longitude_wgs84=longitude,
        horizontal_accuracy_m=horizontal_accuracy_m, horizontal_speed_kmh=horizontal_speed_kmh))

    temporal_kind = V.TemporalKind.INSTANT if lower_us == upper_us else V.TemporalKind.INTERVAL
    store.write_temporal_expression(V.TemporalExpression(
        temporal_expression_id=temporal_expression_id, kind=temporal_kind))
    store.write_temporal_component(V.TemporalComponent(
        temporal_component_id=f"tc-{tag}-lower", temporal_expression_id=temporal_expression_id,
        ordinal=0, part="lower",
        source_field=lower_field, raw_value=str(lower_us)))
    store.write_temporal_component(V.TemporalComponent(
        temporal_component_id=f"tc-{tag}-upper", temporal_expression_id=temporal_expression_id,
        ordinal=1, part="upper",
        source_field=upper_field, raw_value=str(upper_us)))
    store.write_timespan_integration(V.TimespanIntegration(
        timespan_integration_id=f"ti-{tag}", temporal_expression_id=temporal_expression_id,
        method="tz_localize/1.0", lower_unix_utc_us=lower_us, upper_unix_utc_us=upper_us,
        offset_to_local_hours_lower=offset_lower, offset_to_local_hours_upper=offset_upper))

    store.write_observation(V.Observation(
        observation_id=observation_id, raw_record_id=raw_record_id, preset_id=_PRESET_ID, entity_id=entity_id, observation_content_hash=f"content-{tag}",
        spatiotemporal_entity_hash=f"claim-{tag}",
        # A device's own fix: it is the subject, and so its own bearer (decision 55).
        bearer_entity_id=entity_id, entity_link=V.EntityLink.SUBJECT,
        spatial_link=V.SpatialLink.MEASURED,
        # On a degenerate interval box == diamond, so one canonical member is written there.
        temporal_link=(V.TemporalLink.AT_LEAST_ONCE if lower_us == upper_us
                       else V.TemporalLink.THROUGHOUT),
        spatial_expression_id=spatial_expression_id,
        temporal_expression_id=temporal_expression_id))


def build_manual_store(store: RunStore) -> None:
    """Write a small, hand-authored store: TWO observations crafted directly (not via a
    preset run):
      - "instant": a bare device entity (no identifier), a single-instant time
        (lower == upper) — the shape the golden fixtures also use.
      - "interval": a device entity carrying ``MANUAL_IMEI_IDENTIFIER``, a genuine
        interval (lower != upper, 90 minutes apart, offsets differing per bound) —
        the case that expands into two GeoVisualizer rows and exercises the IMEI port.
    Both carry a GNSS fix so kml/geojson/xlsx/geovisualizer all have coordinates to
    round-trip against.
    """
    store.write_case_metadata(V.CaseMetadata(case_id=_CASE_ID, case_number="C-MANUAL"))
    store.write_run(V.Run(run_id=_RUN_ID, case_id=_CASE_ID))
    store.write_preset(V.Preset(
        preset_id=_PRESET_ID, key="test.manual", name="Manual Test Preset",
        version="1.0", definition_digest="digest-manual-1"))
    store.write_source_file(V.SourceFile(
        source_file_id=_SOURCE_ID, run_id=_RUN_ID,
        input_file_path="/manual/source.csv", format="csv"))

    _write_observation(
        store, tag="instant", identifier=None, identifier_type=V.IdentifierType.NONE,
        latitude=46.5197, longitude=6.6323, horizontal_accuracy_m=12.0,
        horizontal_speed_kmh=10.8,
        lower_us=1765026000000000, upper_us=1765026000000000,
        offset_lower=0.0, offset_upper=0.0,
        lower_field="Timestamp", upper_field="Timestamp", line_no=1,
    )
    _write_observation(
        store, tag="interval", identifier=MANUAL_IMEI_IDENTIFIER,
        identifier_type=V.IdentifierType.IMEI,
        latitude=46.2044, longitude=6.1432, horizontal_accuracy_m=8.5,
        horizontal_speed_kmh=None,
        lower_us=1765026000000000, upper_us=1765031400000000,
        # WHY the upper bound's offset is UNKNOWN (not a number): exercises the
        # per-bound "offset unknown" warning GeoVisualizer's description carries
        # (module docstring, F2) — the lower bound stays a known offset so the two
        # emitted rows are distinguishable.
        offset_lower=0.0, offset_upper=V.LOCAL_OFFSET_UNKNOWN,
        lower_field="Start", upper_field="End", line_no=2,
    )
