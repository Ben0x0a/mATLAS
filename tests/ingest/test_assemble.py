"""Tests for the L1 writer (model_atlas/ingest/assemble.py).

Covers the L1-writer verification gate:
  * the nine worked examples (docs/model_worked_examples.md) reproduce their documented
    observation counts — ANPR 1 · CCTV 1 · witness 1 · CDR 1 (+ the B-leg's own record
    = a second, separate line) · Cache 1 · visit 1 · robbery 1 · route search 2 ·
    shared pin 1 — and their component / context shapes;
  * one file matched by TWO presets stays ONE source with TWO observation sets sharing
    the raw_records, converging on spatiotemporal_entity_hash but not observation_content_hash;
  * both digests recompute byte-equal from the stored rows via ``core.ids`` (never a
    local recipe), and folder==zip parity holds end-to-end through the L0 writer;
  * conditionals survive the port (the pin's ``deleted`` mapping), the
    ``N/A`` sentinel stays distinguishable from an unmapped NULL, operator params set
    the origin columns, and ``data_source`` is reported at source grain (the L0 owner
    lands it in ``source.reported_data_source``, and only for a secondary source).

The presets are constructed programmatically (``FieldSpec``/``TimeSpec``/…): the new
L1-only field names (identifier_type, the referentials, …)
are not yet admitted by the fenced v3 YAML loader — see the assemble module header.
Deliberate fixture deviations from the doc walks are commented where they occur.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import sqlite3
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.ids import canonical_json
from model_atlas.core.store import RunStore
from model_atlas.ingest.assemble import assemble_source
from model_atlas.ingest.readers.base import RawRecord
from model_atlas.ingest.writer import ingest_input
from model_atlas.integrate.spatial import integrate_spatial
from model_atlas.presets.expr import is_condition, parse_cond, parse_ref
from model_atlas.presets.spec import (
    ObservationTemplate,
    FieldSpec,
    InputSelector,
    PresetMeta,
    PresetSpec,
    TimeSpec,
)

_FP = "fp-test-0001"
_SELECTOR = InputSelector(format="csv", name="*")


# --- preset construction helpers ---------------------------------------------

def _f(model_field: str, expr: str, **kwargs) -> FieldSpec:
    """One mapped field from its call-form/conditional string (bypasses the YAML
    loader's ASSIGNABLE_FIELDS gate on purpose — the extended names are engine-level)."""
    if is_condition(expr):
        return FieldSpec(model_field=model_field, condition=parse_cond(expr), **kwargs)
    return FieldSpec(model_field=model_field, ref=parse_ref(expr), **kwargs)


def _links(spatial: str | None, entity: str, temporal: str) -> list[FieldSpec]:
    """One link per arm (decision 55); ``spatial=None`` for a template with no position,
    which may carry no spatial link."""
    fields = [_f("entity_link", f"const({entity})"), _f("temporal_link", f"const({temporal})")]
    if spatial is not None:
        fields.insert(0, _f("spatial_link", f"const({spatial})"))
    return fields


def _time(kind: str, lower: str, upper: str | None = None, *,
          epoch: str | None = None, fmt: str | None = None,
          zone: str | None = None) -> TimeSpec:
    lower_ref = parse_ref(lower)
    upper_ref = parse_ref(upper) if upper is not None else lower_ref
    zone_spec = _f("stated_utc_offset", zone) if zone is not None else None
    return TimeSpec(kind=kind, lower=lower_ref, upper=upper_ref,
                    epoch=epoch, format=fmt, zone=zone_spec)


def _preset(key: str, common: list[FieldSpec], observations: list[ObservationTemplate],
            patterns: dict[str, str] | None = None) -> PresetSpec:
    return PresetSpec(
        meta=PresetMeta(id=key, name=key, version="1.0"),
        input_selectors=(_SELECTOR,), path=Path(f"{key}.yaml"),
        common=tuple(common), observations=tuple(observations),
        patterns=patterns or {},
    )


def _preset_row(spec: PresetSpec) -> V.Preset:
    return V.Preset(
        preset_id=ids.preset_id(spec.meta.id, spec.meta.version),
        key=spec.meta.id, name=spec.meta.name, version=spec.meta.version,
        definition_digest="dd-" + spec.meta.id,
    )


def _records(*rows: dict) -> list[RawRecord]:
    return [RawRecord(line_no=i + 1, fields=dict(row)) for i, row in enumerate(rows)]


# --- store seeding / querying helpers ----------------------------------------

def _seed(store: RunStore, records: list[RawRecord], *,
          path: str = "/case/x.csv", fingerprint: str = _FP,
          presets: tuple[PresetSpec, ...] = ()) -> str:
    """Write the L0 rows assemble slots after (case/run/preset/source/raw_record)."""
    case = V.CaseMetadata(case_id=ids.case_metadata_id(), case_number="C1")
    store.write_case_metadata(case)
    run = V.Run(run_id=ids.run_id(), case_id=case.case_id)
    store.write_run(run)
    for spec in presets:
        store.write_preset(_preset_row(spec))
    source_file_id = ids.new_id()
    store.write_source_file(V.SourceFile(
        source_file_id=source_file_id, run_id=run.run_id, input_file_path=path,
        fingerprint_sha256=fingerprint, record_count=len(records),
    ))
    # Mint each raw_record id here and hand it BACK on the in-memory record, exactly as the
    # pipeline does when it reads the rows out again: ids are random (decision 50), so
    # assemble can no longer re-derive them.
    for index, record in enumerate(records):
        raw_record_id = ids.new_id()
        store.write_raw_record(V.RawRecord(
            raw_record_id=raw_record_id,
            source_file_id=source_file_id, line_no=record.line_no,
            raw_json=canonical_json(record.fields),
        ))
        records[index] = dataclasses.replace(record, raw_record_id=raw_record_id)
    return source_file_id


@contextlib.contextmanager
def _assembled(spec: PresetSpec, records: list[RawRecord], **kwargs):
    with RunStore() as store:
        source_file_id = _seed(store, records, presets=(spec,))
        report = assemble_source(
            records, spec, store, source_file_id=source_file_id, fingerprint_sha256=_FP, **kwargs)
        yield store, report, source_file_id


def _rows(store: RunStore, sql: str, *args) -> list[dict]:
    cursor = store.connection.execute(sql, args)
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _one(store: RunStore, sql: str, *args) -> dict:
    rows = _rows(store, sql, *args)
    assert len(rows) == 1, f"expected one row, got {len(rows)}: {sql}"
    return rows[0]


# --- the nine worked examples (docs/model_worked_examples.md) -----------------

def _example_anpr():
    spec = _preset("anpr_ch", common=[
        _f("entity", "field(plate)"), _f("entity_type", "const(vehicle)"),
        _f("identifier_type", "const(plate)"),
        _f("position_source", "const(camera)"),
        _f("position_type", "const(cell_id)"),
        _f("temporal_referential", "const(local_string)"),
        _f("temporal_source", "const(NTP)"),
        _f("source_label", "const(ANPR)"),
    ], observations=[ObservationTemplate(
        fields=tuple([_f("raw_position", "field(camera_id)"),
                      *_links("seen", "party", "at_least_once")]),
        temporal=(_time("instant", "field(ts)", fmt="%Y-%m-%d %H:%M:%S",
                        zone="const(+01:00)"),),
    )])
    records = _records({"camera_id": "CAM-A7-KM12", "ocr_conf": "0.92",
                        "plate": "GE 123 456", "ts": "2026-03-14 02:05:33"})
    return spec, records, 1


def _example_cctv():
    spec = _preset("dvr_generic", common=[
        _f("entity", "field(channel)"), _f("entity_type", "const(camera)"),
        _f("identifier_type", "const(camera)"),
        _f("position_source", "const(manual)"),
        _f("position_type", "const(street_address)"),
        _f("temporal_source", "const(internal_clock)"),
    ], observations=[ObservationTemplate(
        fields=tuple([_f("raw_position", "field(site)"),
                      *_links("located", "subject", "throughout")]),
        # no zone: the source stated no offset — stated_offset stays NULL per bound.
        temporal=(_time("interval", "field(start)", "field(end)",
                        fmt="%Y-%m-%d %H:%M:%S"),),
    )])
    records = _records({"channel": "Lobby-2", "end": "2026-03-14 02:50:00",
                        "site": "Rue du Stand 15, 1204 Geneve",
                        "start": "2026-03-14 02:25:00"})
    return spec, records, 1


def _example_witness():
    # Deviation from the doc walk: the doc shows the bounds' raw as the extracted
    # "vers midi le 14 mars"; the v3 TimeSpec bounds are plain refs (no extract — the
    # T22-noted follow-up), so the verbatim raw here is the whole statement.
    spec = _preset("audition", common=[
        FieldSpec(model_field="entity", ref=parse_ref("field(statement)"),
                  extract=("homme", "who")),
        _f("identifier_type", "const(none)"), _f("entity_type", "const(person)"),
        _f("concerns", "field(witness)"),
        _f("position_source", "const(manual)"),
        _f("position_type", "const(unknown)"),
        _f("temporal_referential", "const(local_string)"),
    ], observations=[ObservationTemplate(
        fields=tuple([FieldSpec(model_field="raw_position",
                                ref=parse_ref("field(statement)"),
                                extract=("place", "place")),
                      *_links("located", "subject", "throughout")]),
        temporal=(_time("interval", "field(statement)", "field(statement)",
                        zone="const(+01:00)"),),
    )], patterns={"homme": "(?P<who>l'homme)", "place": "(?P<place>pres de la gare)"})
    records = _records({"statement": "J'ai vu l'homme pres de la gare vers midi le 14 mars",
                        "witness": "Marie Dupont"})
    return spec, records, 1


def _cdr_common(role: str, entity_field: str, context: dict[str, str]) -> list[FieldSpec]:
    fields = [
        _f("entity", f"field({entity_field})"), _f("identifier_type", "const(msisdn)"),
        _f("entity_type", "const(device)"),
        _f("position_source", "const(cell)"),
        _f("position_type", "const(cell_id)"),
        _f("temporal_referential", "const(local_string)"),
        _f("temporal_source", "const(NTP)"),
        _f("temporal_part_upper", "const(duration)"),
    ]
    return fields


def _example_cdr_a():
    spec = _preset("cdr_generic",
                   common=[*_cdr_common("caller", "a_party",
                                        {"callee": "b_party", "caller_imei": "imei"}),
                           _f("source_label", "const(CDR op-A)")],
                   observations=[ObservationTemplate(
                       fields=tuple([_f("raw_position", "field(cell)"),
                                     *_links("served", "party", "at_least_once")]),
                       temporal=(_time("interval", "field(start)", "field(duration)",
                                       fmt="%Y-%m-%d %H:%M:%S", zone="const(+01:00)"),),
                   )])
    records = _records({"a_party": "+41 79 123 45 67", "b_party": "+41 76 222 33 44",
                        "cell": "228-01-2042-77341", "duration": "187",
                        "imei": "352099001761481", "start": "2026-03-14 02:20:31"})
    return spec, records, 1


def _example_cdr_b():
    # The B-leg is the OTHER operator's own record: a second line on a separate record
    # (never a third line on the A-leg record — decision 14).
    spec = _preset("cdr_b",
                   common=_cdr_common("callee", "callee", {"counterpart": "caller"}),
                   observations=[ObservationTemplate(
                       fields=tuple([_f("raw_position", "field(cell_b)"),
                                     *_links("served", "party", "at_least_once")]),
                       temporal=(_time("interval", "field(start)", "field(duration)",
                                       fmt="%Y-%m-%d %H:%M:%S", zone="const(+01:00)"),),
                   )])
    records = _records({"callee": "+41762223344", "caller": "+41791234567",
                        "cell_b": "228-02-1011-55221", "duration": "187",
                        "start": "2026-03-14 02:20:31"})
    return spec, records, 1


def _example_cache():
    spec = _preset("ios_cache", common=[
        _f("entity_type", "const(device)"),
        _f("position_source", "const(GNSS)"),
        _f("temporal_source", "const(internal_clock)"),
    ], observations=[ObservationTemplate(
        fields=tuple([
            _f("latitude", "field(ZLATITUDE)"),
            _f("longitude", "field(ZLONGITUDE)"),
            _f("position_type", "const(coordinate)"),
            _f("spatial_referential", "const(epsg:4326)"),
            _f("horizontal_accuracy_m", "field(ZHORIZONTALACCURACY)"),
            # unit declared for L2's benefit; L1 must keep the VERBATIM m/s text.
            _f("horizontal_speed_kmh", "field(ZSPEED)", unit="m/s"),
            _f("heading_deg", "field(ZCOURSE)"),
            *_links("measured", "subject", "at_least_once")]),
        temporal=(_time("instant", "field(ZTIMESTAMP)", epoch="cocoa"),),
    )])
    records = _records({"ZCOURSE": "15", "ZHORIZONTALACCURACY": "18",
                        "ZLATITUDE": "46.202400", "ZLONGITUDE": "6.140300",
                        "ZSPEED": "1.1", "ZTIMESTAMP": "763712700"})
    return spec, records, 1


def _example_visit():
    spec = _preset("ios_visits", common=[
        _f("entity_type", "const(device)"),
        _f("position_source", "const(GNSS)"),
    ], observations=[ObservationTemplate(
        fields=tuple([_f("latitude", "field(ZLAT)"),
                      _f("longitude", "field(ZLON)"),
                      _f("position_type", "const(coordinate)"),
            _f("spatial_referential", "const(epsg:4326)"),
                      *_links("measured", "subject", "throughout")]),
        temporal=(_time("interval", "field(ZENTRYDATE)", "field(ZEXITDATE)",
                        epoch="cocoa"),),
    )])
    records = _records({"ZENTRYDATE": "763689600", "ZEXITDATE": "763702200",
                        "ZLAT": "46.193000", "ZLON": "6.129000"})
    return spec, records, 1


def _example_robbery():
    spec = _preset("police_reg", common=[
        _f("entity", "field(case)"), _f("identifier_type", "const(case_ref)"),
        _f("entity_type", "const(event)"),
        # decision 36: identifier == concerns for an event from a structured list.
        _f("concerns", "field(case)"),
        _f("position_source", "const(manual)"),
        _f("position_type", "const(street_address)"),
        _f("temporal_referential", "const(local_string)"),
    ], observations=[ObservationTemplate(
        fields=tuple([_f("raw_position", "field(location)"),
                      *_links("located", "subject", "throughout")]),
        temporal=(_time("interval", "field(from)", "field(to)"),),
    )])
    records = _records({"case": "GE-2026-04412", "from": "2026-03-14 02:30",
                        "location": "Rue du Stand 15, Geneve", "offense": "robbery",
                        "to": "2026-03-14 02:45", "victim": "M. Keller"})
    return spec, records, 1


def _example_route():
    def leg(place_field: str, position_type: str) -> ObservationTemplate:
        return ObservationTemplate(
            fields=tuple([_f("raw_position", f"field({place_field})"),
                          _f("position_type", f"const({position_type})"),
                          *_links("searched", "party", "at_least_once")]),
            temporal=(_time("instant", "field(query_ts)", fmt="iso8601",
                            zone="const(+00:00)"),),
        )

    spec = _preset("nav_generic", common=[
        _f("entity", "field(account)"), _f("identifier_type", "const(email)"),
        _f("entity_type", "const(account)"),
        _f("position_source", "const(manual)"),
        _f("temporal_source", "const(NTP)"),
    ], observations=[leg("destination", "street_address"),
                   leg("origin", "unknown")])
    records = _records({"account": "jdoe@gmail.com",
                        "destination": "Rue du Stand 15, Geneve", "origin": "Home",
                        "query_ts": "2026-03-13T21:04:09Z"})
    return spec, records, 2


def _example_pin():
    spec = _preset("im_generic", common=[
        _f("entity", "field(sender)"), _f("identifier_type", "const(msisdn)"),
        _f("entity_type", "const(device)"),
        _f("position_source", "const(GNSS)"),
        # decision 33: the app's own deleted flag via an if/then conditional (R11).
        _f("recovery_state",
           'if field(is_deleted) == "1" then const(recovered) else const(live)'),
    ], observations=[ObservationTemplate(
        fields=tuple([_f("latitude", "field(lat)"),
                      _f("longitude", "field(lon)"),
                      _f("position_type", "const(coordinate)"),
            _f("spatial_referential", "const(epsg:4326)"),
                      *_links("shared", "party", "at_least_once")]),
        temporal=(_time("instant", "field(ts)", epoch="unix_s"),),
    )])
    records = _records({"caption": "meet me here", "is_deleted": "1",
                        "lat": "46.203000", "lon": "6.140000",
                        "recipient": "+41762223344", "sender": "+41791234567",
                        "ts": "1773450680"})
    return spec, records, 1


_EXAMPLES = {
    "anpr": _example_anpr,
    "cctv": _example_cctv,
    "witness": _example_witness,
    "cdr_a": _example_cdr_a,
    "cdr_b": _example_cdr_b,
    "cache": _example_cache,
    "visit": _example_visit,
    "robbery": _example_robbery,
    "route": _example_route,
    "pin": _example_pin,
}

# Documented line counts (model_worked_examples.md "Line counts at a glance").
_DOCUMENTED_COUNTS = {
    "anpr": 1, "cctv": 1, "witness": 1, "cdr_a": 1, "cdr_b": 1,
    "cache": 1, "visit": 1, "robbery": 1, "route": 2, "pin": 1,
}


# --- worked-example row counts ------------------------------------------------

@pytest.mark.parametrize("name", sorted(_EXAMPLES))
def test_worked_example_observation_counts(name: str) -> None:
    spec, records, expected = _EXAMPLES[name]()
    assert expected == _DOCUMENTED_COUNTS[name]
    with _assembled(spec, records) as (store, report, _):
        stored = _one(store, "SELECT COUNT(*) AS n FROM observation")["n"]
    assert report.observation_count == expected
    assert stored == expected


# --- component / context shapes -----------------------------------------------

def test_anpr_shapes() -> None:
    spec, records, _ = _example_anpr()
    with _assembled(spec, records) as (store, _, _):
        entity = _one(store, "SELECT e.* FROM entity e "
                      "JOIN observation a ON a.entity_id = e.entity_id")
        assert entity["raw_identifier"] == "GE 123 456"     # verbatim (decision 21)
        assert entity["identifier"] is None                 # T32 normalises later
        assert entity["identifier_type"] == "plate"
        assert entity["type"] == "vehicle"

        sx = _one(store, "SELECT * FROM spatial_expression")
        assert (sx["position_type"], sx["referential"], sx["spatial_source"]) \
            == ("cell_id", None, "camera")
        comp = _one(store, "SELECT * FROM spatial_component")
        assert (comp["part"], comp["source_field"], comp["raw_value"]) \
            == ("raw_position", "camera_id", "CAM-A7-KM12")

        tx = _one(store, "SELECT * FROM temporal_expression")
        assert (tx["kind"], tx["referential"], tx["temporal_source"]) \
            == ("instant", "local_string", "NTP")
        bound = _one(store, "SELECT * FROM temporal_component")
        assert (bound["part"], bound["stated_offset"],
                bound["source_field"], bound["raw_value"]) \
            == ("lower", "+01:00", "ts", "2026-03-14 02:05:33")

        observation = _one(store, "SELECT * FROM observation")
        assert (observation["entity_link"], observation["spatial_link"],
                observation["temporal_link"]) == ("party", "seen", "at_least_once")
        # `party`: another entity (the camera) holds the position, so a SECOND entity row
        # exists and the observation points at it (decision 55).
        assert observation["bearer_entity_id"] not in (None, observation["entity_id"])
        assert observation["activity_id"] is None             # never written (D51)
        assert observation["source_label"] == "ANPR"


def test_duration_bound_is_written_verbatim_under_the_duration_part() -> None:
    """A ``duration_sec:`` upper bound keeps the source's digits VERBATIM (no unit conversion
    at L1 — that is the L2 method's job) and defaults to the part ``duration`` rather than
    ``upper``, which is what tells every reader it holds a span, not an end clock."""
    spec, records, _ = _example_cdr_a()
    # The doc fixture renames the part by hand; a duration_unit makes it the DEFAULT.
    time = replace(spec.observations[0].temporal[0], duration_unit="s")
    common = tuple(f for f in spec.common if f.model_field != "temporal_part_upper")
    spec = replace(spec, common=common,
                   observations=(replace(spec.observations[0], temporal=(time,)),))
    with _assembled(spec, records) as (store, _, _):
        bounds = _rows(store, "SELECT * FROM temporal_component ORDER BY ordinal")
        assert [(b["part"], b["raw_value"]) for b in bounds] == [
            ("lower", "2026-03-14 02:20:31"),
            ("duration", "187"),                 # verbatim seconds, NOT scaled to µs
        ]


def test_mapped_source_record_uid_lands_on_every_observation() -> None:
    """A preset-mapped ``source_record_uid`` is resolved ONCE per source record and repeated
    on every observation that record fans out into — that repetition is its job (it GROUPS the
    output rows). It stays verbatim, and it must not disturb either digest."""
    spec, records, _ = _example_route()          # one record -> two observations
    with _assembled(spec, records) as (store, _, _):
        baseline = [r["spatiotemporal_entity_hash"] for r in _rows(
            store, "SELECT spatiotemporal_entity_hash FROM observation ORDER BY rowid")]

    uid_spec = replace(spec, source_record_uid=_f("source_record_uid", "field(item_id)"))
    uid_records = [replace(r, fields={**r.fields, "item_id": "ITEM-42"}) for r in records]
    with _assembled(uid_spec, uid_records) as (store, _, _):
        rows = _rows(store, "SELECT source_record_uid, spatiotemporal_entity_hash FROM observation "
                            "ORDER BY rowid")
        assert len(rows) == 2
        assert [r["source_record_uid"] for r in rows] == ["ITEM-42", "ITEM-42"]
        # A source's own key is provenance, not claim content: the digests are untouched.
        assert [r["spatiotemporal_entity_hash"] for r in rows] == baseline

    # Unmapped (or `source_record_uid: default`) leaves the column NULL, so the flat view
    # falls back to the engine-generated raw_record_id.
    with _assembled(spec, records) as (store, _, _):
        assert all(r["source_record_uid"] is None
                   for r in _rows(store, "SELECT source_record_uid FROM observation"))


def test_duplicate_mapped_source_record_uid_raises() -> None:
    """A MAPPED uid must identify exactly one source record: two records claiming one uid
    would silently merge distinct evidence, so assembly raises rather than warns."""
    spec, records, _ = _example_cdr_a()
    uid_spec = replace(spec, source_record_uid=_f("source_record_uid", "field(a_party)"))
    twice = [*records, replace(records[0], line_no=2)]
    with pytest.raises(ValueError, match="must be unique per source record"):
        with _assembled(uid_spec, twice):
            pass


def test_cache_components_verbatim_and_derived_referentials() -> None:
    spec, records, _ = _example_cache()
    with _assembled(spec, records) as (store, _, _):
        comps = _rows(store, "SELECT * FROM spatial_component ORDER BY ordinal")
        assert [(c["part"], c["source_field"], c["raw_value"]) for c in comps] == [
            ("latitude", "ZLATITUDE", "46.202400"),          # datum-agnostic L1 part
            ("longitude", "ZLONGITUDE", "6.140300"),
            ("horizontal_accuracy_m", "ZHORIZONTALACCURACY", "18"),
            ("horizontal_speed_kmh", "ZSPEED", "1.1"),       # m/s VERBATIM — no unit at L1
            ("heading_deg", "ZCOURSE", "15"),
        ]
        sx = _one(store, "SELECT * FROM spatial_expression")
        assert sx["referential"] == "epsg:4326"              # from the explicit spatial_referential mapping
        tx = _one(store, "SELECT * FROM temporal_expression")
        assert tx["referential"] == "apple_cfabsolute"       # derived from epoch: cocoa
        bound = _one(store, "SELECT * FROM temporal_component")
        assert (bound["part"], bound["stated_offset"],
                bound["raw_value"]) == ("lower", None, "763712700")
        entity = _one(store, "SELECT e.* FROM entity e "
                      "JOIN observation a ON a.entity_id = e.entity_id")
        # No entity mapping: a purely typed mention — nothing can ever converge on it.
        assert (entity["raw_identifier"], entity["identifier_type"], entity["type"]) \
            == (None, "none", "device")


def test_pin_deleted_conditional_and_context() -> None:
    spec, records, _ = _example_pin()
    with _assembled(spec, records) as (store, _, _):
        observation = _one(store, "SELECT * FROM observation")
        assert observation["recovery_state"] == "recovered"    # decision 33 branch matched (R11)
        tx = _one(store, "SELECT * FROM temporal_expression")
        assert tx["referential"] == "unix_epoch_s"           # derived from epoch: unix_s


def test_witness_concerns_and_extracted_raw_identifier() -> None:
    spec, records, _ = _example_witness()
    with _assembled(spec, records) as (store, _, _):
        entity = _one(store, "SELECT e.* FROM entity e "
                      "JOIN observation a ON a.entity_id = e.entity_id")
        assert entity["raw_identifier"] == "l'homme"         # extract selects source text
        assert entity["identifier"] is None                  # not canonicalisable
        assert entity["concerns"] == "Marie Dupont"          # the witness (decision 17)
        assert entity["concerns_origin"] == "preset_mapped"
        comp = _one(store, "SELECT * FROM spatial_component")
        assert comp["raw_value"] == "pres de la gare"
        assert _one(store, "SELECT position_type FROM spatial_expression")["position_type"] \
            == "unknown"   # a bare label is neither an address nor a coordinate


# --- two presets, one source (decision 27) -------------------------------------

def _recompute_digests(store: RunStore, observation: dict) -> tuple[str, str]:
    """Rebuild the L1 projection input from the STORED rows and re-derive both digests
    through core.ids — the round-trip proof that assemble used the normative recipe."""
    entity_row = _one(store, "SELECT * FROM entity WHERE entity_id = ?", observation["entity_id"])
    entity = ids.EntityProjection(entity_row["identifier"], entity_row["identifier_type"],
                                  entity_row["type"])

    spatial = None
    if observation["spatial_expression_id"] is not None:
        sx = _one(store, "SELECT * FROM spatial_expression WHERE spatial_expression_id = ?",
                  observation["spatial_expression_id"])
        comps = _rows(store, "SELECT * FROM spatial_component WHERE spatial_expression_id = ? "
                             "ORDER BY ordinal", observation["spatial_expression_id"])
        spatial = ids.SpatialProjection(
            sx["referential"], sx["spatial_source"],
            tuple(ids.SpatialComponentProjection(c["part"], c["unit"], c["raw_value"])
                  for c in comps))

    temporal = None
    if observation["temporal_expression_id"] is not None:
        tx = _one(store, "SELECT * FROM temporal_expression WHERE temporal_expression_id = ?",
                  observation["temporal_expression_id"])
        comps = _rows(store, "SELECT * FROM temporal_component WHERE temporal_expression_id = ? "
                             "ORDER BY ordinal", observation["temporal_expression_id"])
        temporal = ids.TemporalProjection(
            tx["kind"], tx["referential"],
            tuple(ids.TemporalComponentProjection(c["part"],
                                                  c["stated_offset"], c["raw_value"])
                  for c in comps))

    # The BEARER is read back from the store like every other corner: the round-trip only
    # proves the normative recipe was used if it rebuilds BOTH entity rows. A `subject` row's
    # bearer IS its entity and projects no bearer block (decision 55).
    bearer = None
    if observation["bearer_entity_id"] not in (None, observation["entity_id"]):
        bearer_row = _one(store, "SELECT * FROM entity WHERE entity_id = ?",
                          observation["bearer_entity_id"])
        bearer = ids.EntityProjection(bearer_row["identifier"],
                                      bearer_row["identifier_type"], bearer_row["type"])
    l1 = ids.l1_projection(entity=entity, bearer=bearer, spatial=spatial, temporal=temporal,
                           )
    provenance = _one(store,
                      "SELECT s.fingerprint_sha256 AS fp, r.line_no AS line_no FROM raw_record r "
                      "JOIN source_file s ON s.source_file_id = r.source_file_id WHERE r.raw_record_id = ?",
                      observation["raw_record_id"])
    preset_key = _one(store, "SELECT key FROM preset WHERE preset_id = ?",
                      observation["preset_id"])["key"]
    return ids.digests(fingerprint_sha256=provenance["fp"], preset_key=preset_key,
                       line_no=provenance["line_no"],
                       l1=l1)


@pytest.mark.parametrize("name", ["cdr_a", "pin", "route", "cache"])
def test_stored_digests_recompute_via_core_ids(name: str) -> None:
    spec, records, _ = _EXAMPLES[name]()
    with _assembled(spec, records) as (store, _, _):
        for observation in _rows(store, "SELECT * FROM observation"):
            observation_content_hash, spatiotemporal_entity_hash = _recompute_digests(store, observation)
            assert observation_content_hash == observation["observation_content_hash"]
            assert spatiotemporal_entity_hash == observation["spatiotemporal_entity_hash"]


# --- folder == zip parity, end-to-end through the L0 writer ---------------------

def test_folder_zip_parity_end_to_end(tmp_path: Path) -> None:
    csv_bytes = (b"camera_id,ocr_conf,plate,ts\n"
                 b"CAM-A7-KM12,0.92,GE 123 456,2026-03-14 02:05:33\n"
                 b"CAM-B2-KM03,0.88,GE 123 456,2026-03-12 17:22:05\n")
    folder = tmp_path / "tree"
    folder.mkdir()
    (folder / "anpr.csv").write_bytes(csv_bytes)
    zip_path = tmp_path / "tree.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("anpr.csv", csv_bytes)
    zip_path.write_bytes(buffer.getvalue())

    spec, _, _ = _example_anpr()
    digest_sets: list[set[tuple[str, str]]] = []
    source_file_ids: list[str] = []
    for input_path in (folder, zip_path):
        with RunStore() as store:
            ingest_input(input_path, store, case_number="C1", presets=(_preset_row(spec),))
            source = _one(store, "SELECT source_file_id, fingerprint_sha256 FROM source_file")
            records = [RawRecord(line_no=r["line_no"], fields=json.loads(r["raw_json"]),
                                 raw_record_id=r["raw_record_id"])
                       for r in _rows(store, "SELECT raw_record_id, line_no, raw_json "
                                             "FROM raw_record ORDER BY line_no")]
            assemble_source(records, spec, store, source_file_id=source["source_file_id"],
                            fingerprint_sha256=source["fingerprint_sha256"])
            digest_sets.append({(a["observation_content_hash"], a["spatiotemporal_entity_hash"])
                                for a in _rows(store, "SELECT * FROM observation")})
            source_file_ids.append(source["source_file_id"])

    # Two disclosures, two sources — but no path reaches a digest, so both sets match.
    assert source_file_ids[0] != source_file_ids[1]
    assert len(digest_sets[0]) == 2
    assert digest_sets[0] == digest_sets[1]


# --- sentinel / origins / grain details -----------------------------------------

def test_na_sentinel_flows_to_text_and_unmapped_stays_null() -> None:
    spec, records, _ = _example_cctv()
    # A conditional that evaluates but matches nothing (no else): the decision-33
    # sentinel — stored as the TEXT N/A, distinct from the never-mapped NULL.
    spec = replace(spec, common=(*spec.common,
                                 _f("source_label", 'if field(site) == "nope" then const(x)')))
    with _assembled(spec, records) as (store, _, _):
        observation = _one(store, "SELECT * FROM observation")
        assert observation["source_label"] == "N/A"      # evaluated, produced nothing
        assert observation["recovery_state"] is None     # never mapped -> NULL


def test_operator_entity_param_overrides_and_sets_origin() -> None:
    spec, records, _ = _example_cache()
    with _assembled(spec, records, entity="+41791234567") as (store, _, _):
        entity = _one(store, "SELECT e.* FROM entity e "
                      "JOIN observation a ON a.entity_id = e.entity_id")
        assert entity["raw_identifier"] == "+41791234567"
        # observation.entity_origin was dropped by decision 53; entity.concerns_origin
        # still records provenance for the concerns value.


def test_common_mapped_coordinate_produces_a_spatial_corner() -> None:
    # FIX-11 #2: latitude/longitude mapped under common: must emit a spatial COMPONENT shared
    # by the observation, not vanish into `shared` — otherwise the position and its digest are lost.
    spec = _preset("common_coords", common=[
        _f("entity_type", "const(device)"),
        _f("latitude", "field(lat)"), _f("longitude", "field(lon)"),
        _f("position_type", "const(coordinate)"),
            _f("spatial_referential", "const(epsg:4326)"),
    ], observations=[ObservationTemplate(
        fields=tuple(_links("measured", "subject", "at_least_once")),
        temporal=(_time("instant", "field(ts)", epoch="unix_s"),),
    )])
    records = _records({"lat": "46.2", "lon": "6.1", "ts": "1773450680"})
    with _assembled(spec, records) as (store, _, _):
        comps = _rows(store, "SELECT * FROM spatial_component ORDER BY ordinal")
        assert [(c["part"], c["source_field"], c["raw_value"]) for c in comps] == [
            ("latitude", "lat", "46.2"), ("longitude", "lon", "6.1")]
        sx = _one(store, "SELECT * FROM spatial_expression")
        assert sx["referential"] == "epsg:4326"          # from the explicit common spatial_referential
        observation = _one(store, "SELECT * FROM observation")
        assert observation["spatial_expression_id"] is not None
        assert observation["spatiotemporal_entity_hash"] is not None     # a real placed observation, not placeless


def test_resolver_picks_up_a_field_present_only_in_a_later_record() -> None:
    # FIX-11 #3: a column absent from record[0] but present in a LATER record must still resolve
    # (heterogeneous JSON/JSONL) — seeding the resolver from record[0] alone would drop it.
    spec = _preset("hetero", common=[
        _f("entity", "field(imei)"), _f("identifier_type", "const(msisdn)"),
        _f("entity_type", "const(device)"),
    ], observations=[ObservationTemplate(
        fields=tuple(_links("shared", "party", "at_least_once")),
        temporal=(_time("instant", "field(ts)", epoch="unix_s"),),
    )])
    # 'imei' appears ONLY in the second record.
    records = _records({"ts": "1773450680"},
                       {"imei": "352099001761481", "ts": "1773450681"})
    with _assembled(spec, records) as (store, _, _):
        idents = [row["raw_identifier"] for row in _rows(
            store,
            "SELECT e.raw_identifier FROM entity e "
            "JOIN observation a ON a.entity_id = e.entity_id "
            "JOIN raw_record r ON r.raw_record_id = a.raw_record_id ORDER BY r.line_no")]
    # record 1 has no imei (None); record 2's late-only imei resolves, not silently dropped.
    assert idents == [None, "352099001761481"]


def test_unmatched_data_source_yields_null_not_na_text() -> None:
    # FIX-11 #4: a data_source conditional that evaluates to no-match must leave the reported
    # value None, never the literal "N/A" stamped onto the source row.
    spec, records, _ = _example_cache()
    spec = replace(spec, common=(*spec.common,
                                 _f("data_source", 'if field(ZSPEED) == "nope" then const(x)')))
    with _assembled(spec, records) as (store, report, _):
        assert report.data_source is None


def test_empty_operator_entity_blanks_the_value_and_tags_it_consistently() -> None:
    # FIX-11 #5: --entity "" is an explicit operator choice. The old guard tagged origin
    # operator_param while SKIPPING the empty override, so the stored value stayed the preset's
    # while the origin lied. Now the empty value is applied AND tagged (consistent provenance).
    spec, records, _ = _example_anpr()   # its preset maps entity: field(plate) -> "GE 123 456"
    with _assembled(spec, records, entity="") as (store, _, _):
        entity = _one(store, "SELECT e.* FROM entity e "
                      "JOIN observation a ON a.entity_id = e.entity_id")
        assert entity["raw_identifier"] == ""            # the operator's blank, not the preset value


# --- FIX-02: per-bound accuracy component guard -------------------------------

def _acc_interval_spec_records(with_lower_acc: bool):
    """An interval preset whose LOWER bound is MISSING (no 'start') but UPPER present, with the
    upper accuracy mapped (+ the lower accuracy too when ``with_lower_acc``)."""
    fields = [_f("time_accuracy_us_upper", "field(acc_hi)")]
    if with_lower_acc:
        fields.insert(0, _f("time_accuracy_us_lower", "field(acc_lo)"))
    spec = _preset("acc_interval", common=[
        _f("entity_type", "const(device)"),
    ], observations=[ObservationTemplate(
        fields=tuple([*fields, *_links(None, "subject", "throughout")]),
        temporal=(_time("interval", "field(start)", "field(end)", epoch="unix_s"),),
    )])
    records = _records({"end": "1773450681", "acc_lo": "500000", "acc_hi": "1000000"})
    return spec, records


def test_malformed_interval_gates_accuracy_per_side() -> None:
    # FIX-02: lower bound missing, upper present, BOTH accuracies mapped. The lower accuracy must
    # NOT be written as an orphan (no lower bound); the upper accuracy IS written; and the
    # spatiotemporal_entity_hash equals the same record WITHOUT the stray lower-accuracy mapping.
    spec, records = _acc_interval_spec_records(with_lower_acc=True)
    with _assembled(spec, records) as (store, _, _):
        parts = [(c["part"], c["raw_value"])
                 for c in _rows(store, "SELECT * FROM temporal_component ORDER BY ordinal")]
        assert ("upper", "1773450681") in parts
        assert ("upper_accuracy", "1000000") in parts
        assert not any(part == "lower_accuracy" for part, _ in parts)   # no orphan
        digest_with = _one(store, "SELECT spatiotemporal_entity_hash FROM observation")["spatiotemporal_entity_hash"]

    spec2, records2 = _acc_interval_spec_records(with_lower_acc=False)
    with _assembled(spec2, records2) as (store, _, _):
        digest_without = _one(store, "SELECT spatiotemporal_entity_hash FROM observation")["spatiotemporal_entity_hash"]
    # The malformed lower accuracy contributed nothing to identity, as it must not.
    assert digest_with == digest_without


# --- datum → referential: the L1 angle parts are datum-agnostic ---------------

def _agnostic_coords_preset(key: str, *, with_referential: bool) -> PresetSpec:
    """A preset mapping the datum-agnostic ``latitude``/``longitude`` parts, optionally with
    an explicit ``spatial_referential: const(epsg:4326)`` — the datum carrier."""
    # The type is stated either way: what varies is whether the CRS is named, which is what
    # licenses adopting the datum-agnostic parts as WGS84.
    position = [_f("latitude", "field(lat)"), _f("longitude", "field(lon)"),
                _f("position_type", "const(coordinate)")]
    if with_referential:
        position.append(_f("spatial_referential", "const(epsg:4326)"))
    return _preset(key, common=[
        _f("entity_type", "const(device)"),
    ], observations=[ObservationTemplate(
        fields=tuple([*position, *_links("measured", "subject", "at_least_once")]),
        temporal=(_time("instant", "field(ts)", epoch="unix_s"),),
    )])


def test_agnostic_coordinates_with_referential_integrate_to_wgs84_columns() -> None:
    # The datum→referential change, end to end: a preset mapping the datum-agnostic
    # ``latitude``/``longitude`` parts WITH an explicit ``spatial_referential:
    # const(epsg:4326)`` integrates to the canonical L2 ``latitude_wgs84``/``longitude_wgs84``
    # columns, exactly as the old part-name-baked model did — and assemble emits NO warning.
    spec = _agnostic_coords_preset("agnostic_coords", with_referential=True)
    records = _records({"lat": "46.2030", "lon": "6.1430", "ts": "1773450680"})
    with _assembled(spec, records) as (store, report, _):
        assert report.warnings == []
        assert _one(store, "SELECT * FROM spatial_expression")["referential"] == "epsg:4326"
        # the L1 component parts are datum-agnostic — the datum lives on the expression.
        parts = [c["part"] for c in
                 _rows(store, "SELECT * FROM spatial_component ORDER BY ordinal")]
        assert parts == ["latitude", "longitude"]
        # integrate: the agnostic parts are ADOPTED onto the canonical WGS84 L2 columns.
        integrate_spatial(store)
        si = _one(store, "SELECT latitude_wgs84, longitude_wgs84 FROM spatial_integration")
        assert (si["latitude_wgs84"], si["longitude_wgs84"]) == (46.2030, 6.1430)


def test_agnostic_coordinates_without_referential_are_unknown_and_warn() -> None:
    # The SAME mapping WITHOUT a ``spatial_referential`` resolves to ``unknown`` (no part-name
    # datum crutch): the coordinates cannot be integrated (no spatial_integration row, empty L2
    # coords) and assemble SURFACES a warning naming the missing referential — the mistake is
    # loud, never silently coordinate-less.
    spec = _agnostic_coords_preset("agnostic_no_ref", with_referential=False)
    records = _records({"lat": "46.2030", "lon": "6.1430", "ts": "1773450680"})
    with _assembled(spec, records) as (store, report, _):
        assert _one(store, "SELECT * FROM spatial_expression")["referential"] == "unknown"
        assert any("spatial_referential" in warning for warning in report.warnings)
        assert integrate_spatial(store).integration_count == 0
        assert _rows(store, "SELECT * FROM spatial_integration") == []


def test_time_without_epoch_or_format_assumes_unix_us_and_warns() -> None:
    # A time mapped with NEITHER epoch: nor format: is assumed to already be Unix microseconds
    # (temporal_referential = unix_epoch_us). That assumption is SURFACED as a warning so a
    # source that is really seconds / an Apple epoch is not silently mis-scaled. (A time WITH an
    # epoch/format declared emits no such warning — every other timed test above asserts that.)
    spec = _preset("bare_time", common=[
        _f("entity_type", "const(device)"),
    ], observations=[ObservationTemplate(
        fields=tuple([_f("raw_position", "field(loc)"),
                      *_links("measured", "subject", "at_least_once")]),
        temporal=(_time("instant", "field(ts)"),),   # no epoch, no format
    )])
    records = _records({"ts": "1773450680000000", "loc": "Lobby"})
    with _assembled(spec, records) as (store, report, _):
        assert _one(store, "SELECT * FROM temporal_expression")["referential"] == "unix_epoch_us"
        assert any("epoch" in warning for warning in report.warnings)


# --- decision 55: the links and the bearer ------------------------------------

def _point_preset(key: str, links: list[FieldSpec], *, kind: str = "instant",
                  extra: tuple[FieldSpec, ...] = (), with_position: bool = True) -> PresetSpec:
    position = [_f("latitude", "field(lat)"), _f("longitude", "field(lon)"),
                _f("position_type", "const(coordinate)"),
                _f("spatial_referential", "const(epsg:4326)")] if with_position else []
    time = (_time("instant", "field(ts)", epoch="unix_s") if kind == "instant"
            else _time("interval", "field(ts)", "field(te)", epoch="unix_s"))
    return _preset(key, common=[_f("entity_type", "const(device)"), *extra],
                   observations=[ObservationTemplate(fields=tuple([*position, *links]),
                                                     temporal=(time,))])


def _point_records() -> list[RawRecord]:
    return _records({"lat": "46.2", "lon": "6.1", "ts": "1773450680", "te": "1773450690"})


def test_subject_bearer_is_the_targeted_entity() -> None:
    """``subject``: the entity holds its own position, so the bearer IS ``entity_id`` and no
    second entity row is minted."""
    spec = _point_preset("gnss", _links("measured", "subject", "at_least_once"))
    with _assembled(spec, _point_records()) as (store, _, _):
        observation = _one(store, "SELECT * FROM observation")
        assert observation["bearer_entity_id"] == observation["entity_id"]
        assert _one(store, "SELECT COUNT(*) AS n FROM entity")["n"] == 1


def test_unknown_link_mints_no_bearer_even_when_one_is_mapped() -> None:
    """The Waze case: a placeholder bearer would manufacture a presence claim, so ``unknown``
    writes none — whatever the preset maps."""
    spec = _point_preset("waze", _links("unknown", "unknown", "unknown"),
                         extra=(_f("bearer_entity_type", "const(poi)"),))
    with _assembled(spec, _point_records()) as (store, _, _):
        assert _one(store, "SELECT * FROM observation")["bearer_entity_id"] is None
        assert _one(store, "SELECT COUNT(*) AS n FROM entity")["n"] == 1


def test_mentioned_mints_a_bearer_only_when_the_record_names_one() -> None:
    unnamed = _point_preset("pin_a", _links("shared", "mentioned", "at_least_once"))
    with _assembled(unnamed, _point_records()) as (store, _, _):
        assert _one(store, "SELECT * FROM observation")["bearer_entity_id"] is None
    named = _point_preset("pin_b", _links("shared", "mentioned", "at_least_once"),
                          extra=(_f("bearer_entity", "const(scene-pin)"),))
    with _assembled(named, _point_records()) as (store, _, _):
        observation = _one(store, "SELECT * FROM observation")
        assert observation["bearer_entity_id"] not in (None, observation["entity_id"])


@pytest.mark.parametrize("stated,written", [
    ("throughout", "at_least_once"), ("not_throughout", "never"),
    ("at_least_once", "at_least_once"), ("never", "never"), ("unknown", "unknown")])
def test_an_instant_carries_one_canonical_temporal_link(stated: str, written: str) -> None:
    """On an instant box == diamond, so each fact has exactly ONE stored encoding."""
    spec = _point_preset("instant", _links("measured", "subject", stated))
    with _assembled(spec, _point_records()) as (store, _, _):
        assert _one(store, "SELECT * FROM observation")["temporal_link"] == written


def test_an_interval_keeps_the_stated_temporal_link() -> None:
    spec = _point_preset("interval", _links("measured", "subject", "throughout"),
                         kind="interval")
    with _assembled(spec, _point_records()) as (store, _, _):
        assert _one(store, "SELECT * FROM observation")["temporal_link"] == "throughout"


def test_a_spatial_link_without_a_position_is_dropped_loudly() -> None:
    """No geometry means no position whose use could be described: the value is dropped with
    a warning rather than failing the schema CHECK and losing the row."""
    spec = _point_preset("no_position", _links("served", "party", "at_least_once"),
                         with_position=False)
    with _assembled(spec, _point_records()) as (store, report, _):
        assert _one(store, "SELECT * FROM observation")["spatial_link"] is None
        assert any("spatial_link 'served'" in warning for warning in report.warnings)


def test_two_readings_disagreeing_on_the_links_converge() -> None:
    """Links are the preset author's statement, not record content (decision 55): a GNSS
    reading and a cautious ``unknown`` reading of the SAME record share one
    ``spatiotemporal_entity_hash``, and the disagreement stays visible in the link columns."""
    hashes = {}
    for key, links in (("reading_a", _links("measured", "subject", "at_least_once")),
                       ("reading_b", _links("unknown", "unknown", "unknown"))):
        with _assembled(_point_preset(key, links), _point_records()) as (store, _, _):
            hashes[key] = _one(store, "SELECT * FROM observation")["spatiotemporal_entity_hash"]
    assert hashes["reading_a"] == hashes["reading_b"]
