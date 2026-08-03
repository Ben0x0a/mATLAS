"""Tests for the L1 writer (model_atlas/ingest/assemble.py).

Covers the L1-writer verification gate:
  * the nine worked examples (docs/model_worked_examples.md) reproduce their documented
    assertion counts — ANPR 1 · CCTV 1 · witness 1 · CDR 1 (+ the B-leg's own record
    = a second, separate line) · Cache 1 · visit 1 · robbery 1 · route search 2 ·
    shared pin 1 — and their component / context shapes;
  * one file matched by TWO presets stays ONE source with TWO assertion sets sharing
    the raw_records, converging on claim_digest but not content_hash;
  * both digests recompute byte-equal from the stored rows via ``core.ids`` (never a
    local recipe), and folder==zip parity holds end-to-end through the L0 writer;
  * conditionals survive the port (the pin's ``deleted`` mapping), the
    ``N/A`` sentinel stays distinguishable from an unmapped NULL, operator params set
    the origin columns, and ``data_source`` is reported at source grain.

The presets are constructed programmatically (``FieldSpec``/``TimeSpec``/…): the new
L1-only field names (entity_role, identifier_type, activity_kind, context.<key>, …)
are not yet admitted by the fenced v3 YAML loader — see the assemble module header.
Deliberate fixture deviations from the doc walks are commented where they occur.
"""
from __future__ import annotations

import contextlib
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
    AssertionTemplate,
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


def _links(epl: str, etl: str, stl: str) -> list[FieldSpec]:
    return [
        _f("entity_position_link", f"const({epl})"),
        _f("entity_time_link", f"const({etl})"),
        _f("spatial_temporal_link", f"const({stl})"),
    ]


def _time(kind: str, lower: str, upper: str | None = None, *,
          epoch: str | None = None, fmt: str | None = None,
          zone: str | None = None) -> TimeSpec:
    lower_ref = parse_ref(lower)
    upper_ref = parse_ref(upper) if upper is not None else lower_ref
    zone_spec = _f("utc_offset_hours", zone) if zone is not None else None
    return TimeSpec(kind=kind, lower=lower_ref, upper=upper_ref,
                    epoch=epoch, format=fmt, zone=zone_spec)


def _preset(key: str, common: list[FieldSpec], assertions: list[AssertionTemplate],
            patterns: dict[str, str] | None = None) -> PresetSpec:
    return PresetSpec(
        meta=PresetMeta(id=key, name=key, version="1.0"),
        input_selectors=(_SELECTOR,), path=Path(f"{key}.yaml"),
        common=tuple(common), assertions=tuple(assertions),
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
    source_id = ids.source_id(path, None, None)
    store.write_source(V.Source(
        source_id=source_id, run_id=run.run_id, input_file_path=path,
        fingerprint_sha256=fingerprint, record_count=len(records),
    ))
    for record in records:
        store.write_raw_record(V.RawRecord(
            raw_record_id=ids.raw_record_id(source_id, record.line_no),
            source_id=source_id, line_no=record.line_no,
            raw_json=canonical_json(record.fields),
        ))
    return source_id


@contextlib.contextmanager
def _assembled(spec: PresetSpec, records: list[RawRecord], **kwargs):
    with RunStore() as store:
        source_id = _seed(store, records, presets=(spec,))
        report = assemble_source(
            records, spec, store, source_id=source_id, fingerprint_sha256=_FP, **kwargs)
        yield store, report, source_id


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
        _f("identifier_type", "const(plate)"), _f("entity_role", "const(subject)"),
        _f("position_source", "const(camera)"), _f("position_type", "const(vicinity)"),
        _f("spatial_referential", "const(cell_ref)"),
        _f("temporal_referential", "const(local_string)"),
        _f("temporal_source", "const(NTP)"), _f("temporal_role_lower", "const(last_seen)"),
        _f("source_label", "const(ANPR)"),
    ], assertions=[AssertionTemplate(
        fields=tuple([_f("raw_position", "field(camera_id)"),
                      *_links("at_fixed_detector", "observed_at", "instant")]),
        temporal=(_time("instant", "field(ts)", fmt="%Y-%m-%d %H:%M:%S",
                        zone="const(+01:00)"),),
    )])
    records = _records({"camera_id": "CAM-A7-KM12", "ocr_conf": "0.92",
                        "plate": "GE 123 456", "ts": "2026-03-14 02:05:33"})
    return spec, records, 1


def _example_cctv():
    spec = _preset("dvr_generic", common=[
        _f("entity", "field(channel)"), _f("entity_type", "const(camera)"),
        _f("identifier_type", "const(camera)"), _f("entity_role", "const(subject)"),
        _f("position_source", "const(manual)"), _f("position_type", "const(vicinity)"),
        _f("spatial_referential", "const(address)"),
        _f("temporal_source", "const(internal_clock)"),
        _f("temporal_role_lower", "const(start)"), _f("temporal_role_upper", "const(end)"),
    ], assertions=[AssertionTemplate(
        fields=tuple([_f("raw_position", "field(site)"),
                      *_links("at", "recorded_at", "continuous_during_interval")]),
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
        _f("entity_role", "const(reporter)"), _f("concerns", "field(witness)"),
        _f("position_source", "const(manual)"), _f("position_type", "const(vicinity)"),
        _f("spatial_referential", "const(place_name)"),
        _f("temporal_referential", "const(local_string)"),
        _f("temporal_role_lower", "const(start)"), _f("temporal_role_upper", "const(end)"),
    ], assertions=[AssertionTemplate(
        fields=tuple([FieldSpec(model_field="raw_position",
                                ref=parse_ref("field(statement)"),
                                extract=("place", "place")),
                      *_links("claimed_at", "reported_for", "continuous_during_interval")]),
        temporal=(_time("interval", "field(statement)", "field(statement)",
                        zone="const(+01:00)"),),
    )], patterns={"homme": "(?P<who>l'homme)", "place": "(?P<place>pres de la gare)"})
    records = _records({"statement": "J'ai vu l'homme pres de la gare vers midi le 14 mars",
                        "witness": "Marie Dupont"})
    return spec, records, 1


def _cdr_common(role: str, entity_field: str, context: dict[str, str]) -> list[FieldSpec]:
    fields = [
        _f("activity_kind", "const(call)"),
        _f("entity", f"field({entity_field})"), _f("identifier_type", "const(msisdn)"),
        _f("entity_type", "const(device)"), _f("entity_role", f"const({role})"),
        _f("position_source", "const(cell)"), _f("position_type", "const(vicinity)"),
        _f("spatial_referential", "const(cell_ref)"),
        _f("temporal_referential", "const(local_string)"),
        _f("temporal_source", "const(NTP)"),
        _f("temporal_role_lower", "const(start)"), _f("temporal_role_upper", "const(end)"),
        _f("temporal_part_upper", "const(duration)"),
    ]
    fields.extend(_f(f"context.{key}", f"field({source})") for key, source in context.items())
    return fields


def _example_cdr_a():
    spec = _preset("cdr_generic",
                   common=[*_cdr_common("caller", "a_party",
                                        {"callee": "b_party", "caller_imei": "imei"}),
                           _f("source_label", "const(CDR op-A)")],
                   assertions=[AssertionTemplate(
                       fields=tuple([_f("raw_position", "field(cell)"),
                                     *_links("within_range_of", "event_at",
                                             "continuous_during_interval")]),
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
                   assertions=[AssertionTemplate(
                       fields=tuple([_f("raw_position", "field(cell_b)"),
                                     *_links("within_range_of", "event_at",
                                             "continuous_during_interval")]),
                       temporal=(_time("interval", "field(start)", "field(duration)",
                                       fmt="%Y-%m-%d %H:%M:%S", zone="const(+01:00)"),),
                   )])
    records = _records({"callee": "+41762223344", "caller": "+41791234567",
                        "cell_b": "228-02-1011-55221", "duration": "187",
                        "start": "2026-03-14 02:20:31"})
    return spec, records, 1


def _example_cache():
    spec = _preset("ios_cache", common=[
        _f("entity_type", "const(device)"), _f("entity_role", "const(subject)"),
        _f("position_source", "const(GNSS)"),
        _f("temporal_source", "const(internal_clock)"),
        _f("temporal_role_lower", "const(last_seen)"),
    ], assertions=[AssertionTemplate(
        fields=tuple([
            _f("latitude", "field(ZLATITUDE)"),
            _f("longitude", "field(ZLONGITUDE)"),
            _f("spatial_referential", "const(epsg:4326)"),
            _f("horizontal_accuracy_m", "field(ZHORIZONTALACCURACY)"),
            # unit declared for L2's benefit; L1 must keep the VERBATIM m/s text.
            _f("horizontal_speed_kmh", "field(ZSPEED)", unit="m/s"),
            _f("heading_deg", "field(ZCOURSE)"),
            *_links("at", "observed_at", "instant")]),
        temporal=(_time("instant", "field(ZTIMESTAMP)", epoch="cocoa"),),
    )])
    records = _records({"ZCOURSE": "15", "ZHORIZONTALACCURACY": "18",
                        "ZLATITUDE": "46.202400", "ZLONGITUDE": "6.140300",
                        "ZSPEED": "1.1", "ZTIMESTAMP": "763712700"})
    return spec, records, 1


def _example_visit():
    spec = _preset("ios_visits", common=[
        _f("entity_type", "const(device)"), _f("entity_role", "const(subject)"),
        _f("position_source", "const(GNSS)"),
        _f("temporal_role_lower", "const(check_in)"),
        _f("temporal_role_upper", "const(check_out)"),
    ], assertions=[AssertionTemplate(
        fields=tuple([_f("latitude", "field(ZLAT)"),
                      _f("longitude", "field(ZLON)"),
                      _f("spatial_referential", "const(epsg:4326)"),
                      *_links("at", "observed_at", "continuous_during_interval")]),
        temporal=(_time("interval", "field(ZENTRYDATE)", "field(ZEXITDATE)",
                        epoch="cocoa"),),
    )])
    records = _records({"ZENTRYDATE": "763689600", "ZEXITDATE": "763702200",
                        "ZLAT": "46.193000", "ZLON": "6.129000"})
    return spec, records, 1


def _example_robbery():
    spec = _preset("police_reg", common=[
        _f("entity", "field(case)"), _f("identifier_type", "const(case_ref)"),
        _f("entity_type", "const(event)"), _f("entity_role", "const(subject)"),
        # decision 36: identifier == concerns for an event from a structured list.
        _f("concerns", "field(case)"),
        _f("position_source", "const(manual)"), _f("position_type", "const(vicinity)"),
        _f("spatial_referential", "const(address)"),
        _f("temporal_referential", "const(local_string)"),
        _f("temporal_role_lower", "const(start)"), _f("temporal_role_upper", "const(end)"),
    ], assertions=[AssertionTemplate(
        fields=tuple([_f("raw_position", "field(location)"),
                      *_links("claimed_at", "reported_for", "continuous_during_interval")]),
        temporal=(_time("interval", "field(from)", "field(to)"),),
    )])
    records = _records({"case": "GE-2026-04412", "from": "2026-03-14 02:30",
                        "location": "Rue du Stand 15, Geneve", "offense": "robbery",
                        "to": "2026-03-14 02:45", "victim": "M. Keller"})
    return spec, records, 1


def _example_route():
    def leg(place_field: str, role: str, referential: str) -> AssertionTemplate:
        return AssertionTemplate(
            fields=tuple([_f("raw_position", f"field({place_field})"),
                          _f("position_type", f"const({role})"),
                          _f("spatial_referential", f"const({referential})"),
                          *_links("references", "event_at", "instant")]),
            temporal=(_time("instant", "field(query_ts)", fmt="iso8601",
                            zone="const(+00:00)"),),
        )

    spec = _preset("nav_generic", common=[
        _f("activity_kind", "const(trip)"),
        _f("context.origin", "field(origin)"),
        _f("context.destination", "field(destination)"),
        _f("entity", "field(account)"), _f("identifier_type", "const(email)"),
        _f("entity_type", "const(account)"), _f("entity_role", "const(subject)"),
        _f("position_source", "const(manual)"),
        _f("temporal_source", "const(NTP)"), _f("temporal_role_lower", "const(start)"),
    ], assertions=[leg("destination", "destination", "address"),
                   leg("origin", "origin", "place_name")])
    records = _records({"account": "jdoe@gmail.com",
                        "destination": "Rue du Stand 15, Geneve", "origin": "Home",
                        "query_ts": "2026-03-13T21:04:09Z"})
    return spec, records, 2


def _example_pin():
    spec = _preset("im_generic", common=[
        _f("activity_kind", "const(message)"),
        _f("context.recipient", "field(recipient)"),
        _f("entity", "field(sender)"), _f("identifier_type", "const(msisdn)"),
        _f("entity_type", "const(device)"), _f("entity_role", "const(sender)"),
        _f("position_source", "const(GNSS)"),
        # decision 33: the app's own deleted flag via an if/then conditional (R11).
        _f("deleted", 'if field(is_deleted) == "1" then const(deleted) else const(live)'),
    ], assertions=[AssertionTemplate(
        fields=tuple([_f("latitude", "field(lat)"),
                      _f("longitude", "field(lon)"),
                      _f("spatial_referential", "const(epsg:4326)"),
                      *_links("references", "event_at", "instant")]),
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
def test_worked_example_assertion_counts(name: str) -> None:
    spec, records, expected = _EXAMPLES[name]()
    assert expected == _DOCUMENTED_COUNTS[name]
    with _assembled(spec, records) as (store, report, _):
        stored = _one(store, "SELECT COUNT(*) AS n FROM assertion")["n"]
    assert report.assertion_count == expected
    assert stored == expected


# --- component / context shapes -----------------------------------------------

def test_anpr_shapes() -> None:
    spec, records, _ = _example_anpr()
    with _assembled(spec, records) as (store, _, _):
        entity = _one(store, "SELECT * FROM entity")
        assert entity["raw_identifier"] == "GE 123 456"     # verbatim (decision 21)
        assert entity["identifier"] is None                 # T32 normalises later
        assert entity["identifier_type"] == "plate"
        assert entity["type"] == "vehicle"

        sx = _one(store, "SELECT * FROM spatial_expression")
        assert (sx["referential"], sx["spatial_source"]) == ("cell_ref", "camera")
        comp = _one(store, "SELECT * FROM spatial_component")
        assert (comp["part"], comp["spatial_role"], comp["source_field"], comp["raw_value"]) \
            == ("raw_position", "vicinity", "camera_id", "CAM-A7-KM12")

        tx = _one(store, "SELECT * FROM temporal_expression")
        assert (tx["kind"], tx["referential"], tx["temporal_source"]) \
            == ("instant", "local_string", "NTP")
        bound = _one(store, "SELECT * FROM temporal_component")
        assert (bound["part"], bound["temporal_role"], bound["stated_offset"],
                bound["source_field"], bound["raw_value"]) \
            == ("lower", "last_seen", "+01:00", "ts", "2026-03-14 02:05:33")

        assertion = _one(store, "SELECT * FROM assertion")
        assert (assertion["entity_role"], assertion["entity_position_link"],
                assertion["entity_time_link"], assertion["spatial_temporal_link"]) \
            == ("subject", "at_fixed_detector", "observed_at", "instant")
        assert assertion["activity_id"] is None             # nothing to group (D35)
        assert assertion["source_label"] == "ANPR"


def test_time_bound_extract_pulls_substring_from_free_text() -> None:
    """A time bound's ``extract`` selects the time out of a free-text statement, so the
    temporal component stores "14:00" verbatim, not the whole sentence."""
    spec = _preset(
        "witness_time",
        common=[_f("entity", "const(witness)"), _f("identifier_type", "const(none)")],
        assertions=[AssertionTemplate(
            fields=tuple(_links("claimed_at", "observed_at", "instant")),
            temporal=(TimeSpec(
                kind="instant",
                lower=parse_ref("field(statement)"), upper=parse_ref("field(statement)"),
                format="%H:%M",
                lower_extract=("clock", "t"), upper_extract=("clock", "t")),),
        )],
        patterns={"clock": r"(?P<t>\d{1,2}:\d{2})"},
    )
    records = _records({"statement": "I saw him near the station around 14:00"})
    with _assembled(spec, records) as (store, _, _):
        bound = _one(store, "SELECT * FROM temporal_component")
        assert bound["raw_value"] == "14:00"    # the selection, not the whole statement


def test_cdr_activity_context_and_duration_part() -> None:
    spec, records, _ = _example_cdr_a()
    with _assembled(spec, records) as (store, report, _):
        activity = _one(store, "SELECT * FROM activity")
        assert activity["kind"] == "call"
        # decision 18: preset-mapped context, canonical JSON. Values stay VERBATIM here
        # (the demo normalised them by hand; normalisation is the L2/T32 business).
        assert json.loads(activity["context_json"]) == {
            "callee": "+41 76 222 33 44", "caller_imei": "352099001761481"}
        # R7 invariant: exactly ONE activity_record link at ingest — its own record.
        links = _rows(store, "SELECT * FROM activity_record")
        assert len(links) == 1
        assert links[0]["activity_id"] == activity["activity_id"]
        assert report.activity_count == 1

        bounds = _rows(store, "SELECT * FROM temporal_component ORDER BY ordinal")
        assert [(b["part"], b["temporal_role"], b["stated_offset"], b["raw_value"])
                for b in bounds] == [
            ("lower", "start", "+01:00", "2026-03-14 02:20:31"),
            ("duration", "end", "+01:00", "187"),           # upper renamed (decision 23)
        ]
        assertion = _one(store, "SELECT * FROM assertion")
        assert assertion["activity_id"] == activity["activity_id"]
        assert assertion["entity_role"] == "caller"


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
        assert all(c["spatial_role"] is None for c in comps)
        sx = _one(store, "SELECT * FROM spatial_expression")
        assert sx["referential"] == "epsg:4326"              # from the explicit spatial_referential mapping
        tx = _one(store, "SELECT * FROM temporal_expression")
        assert tx["referential"] == "apple_cfabsolute"       # derived from epoch: cocoa
        bound = _one(store, "SELECT * FROM temporal_component")
        assert (bound["part"], bound["temporal_role"], bound["stated_offset"],
                bound["raw_value"]) == ("lower", "last_seen", None, "763712700")
        entity = _one(store, "SELECT * FROM entity")
        # No entity mapping: a purely typed mention — nothing can ever converge on it.
        assert (entity["raw_identifier"], entity["identifier_type"], entity["type"]) \
            == (None, "none", "device")


def test_visit_per_bound_roles() -> None:
    spec, records, _ = _example_visit()
    with _assembled(spec, records) as (store, _, _):
        bounds = _rows(store, "SELECT * FROM temporal_component ORDER BY ordinal")
        # decision 25: each bound carries its OWN role — check_in / check_out.
        assert [(b["part"], b["temporal_role"], b["source_field"], b["raw_value"])
                for b in bounds] == [
            ("lower", "check_in", "ZENTRYDATE", "763689600"),
            ("upper", "check_out", "ZEXITDATE", "763702200"),
        ]
        assert _one(store, "SELECT kind FROM temporal_expression")["kind"] == "interval"


def test_route_two_assertions_share_one_activity() -> None:
    spec, records, _ = _example_route()
    with _assembled(spec, records) as (store, _, _):
        assertions = _rows(store, "SELECT * FROM assertion ORDER BY output_ordinal")
        assert [a["output_ordinal"] for a in assertions] == [0, 1]
        assert assertions[0]["activity_id"] == assertions[1]["activity_id"]
        activity = _one(store, "SELECT * FROM activity")
        assert activity["kind"] == "trip"
        assert json.loads(activity["context_json"]) == {
            "destination": "Rue du Stand 15, Geneve", "origin": "Home"}
        comps = _rows(store,
                      "SELECT sc.* FROM spatial_component sc "
                      "JOIN assertion a ON a.spatial_expression_id = sc.spatial_expression_id "
                      "ORDER BY a.output_ordinal")
        # decision 25: each leg's value-group carries its own spatial_role.
        assert [(c["spatial_role"], c["raw_value"]) for c in comps] == [
            ("destination", "Rue du Stand 15, Geneve"), ("origin", "Home")]
        referentials = _rows(store,
                             "SELECT sx.referential FROM spatial_expression sx "
                             "JOIN assertion a ON a.spatial_expression_id = sx.spatial_expression_id "
                             "ORDER BY a.output_ordinal")
        assert [r["referential"] for r in referentials] == ["address", "place_name"]


def test_pin_deleted_conditional_and_context() -> None:
    spec, records, _ = _example_pin()
    with _assembled(spec, records) as (store, _, _):
        assertion = _one(store, "SELECT * FROM assertion")
        assert assertion["deleted"] == "deleted"             # decision 33 branch matched (R11)
        assert assertion["entity_role"] == "sender"
        activity = _one(store, "SELECT * FROM activity")
        assert activity["kind"] == "message"
        assert json.loads(activity["context_json"]) == {"recipient": "+41762223344"}
        tx = _one(store, "SELECT * FROM temporal_expression")
        assert tx["referential"] == "unix_epoch_s"           # derived from epoch: unix_s


def test_witness_concerns_and_extracted_raw_identifier() -> None:
    spec, records, _ = _example_witness()
    with _assembled(spec, records) as (store, _, _):
        entity = _one(store, "SELECT * FROM entity")
        assert entity["raw_identifier"] == "l'homme"         # extract selects source text
        assert entity["identifier"] is None                  # not canonicalisable
        assert entity["concerns"] == "Marie Dupont"          # the witness (decision 17)
        assert entity["concerns_origin"] == "preset_mapped"
        comp = _one(store, "SELECT * FROM spatial_component")
        assert comp["raw_value"] == "pres de la gare"
        assert _one(store, "SELECT referential FROM spatial_expression")["referential"] \
            == "place_name"


# --- two presets, one source (decision 27) -------------------------------------

def test_two_presets_one_source_two_assertion_sets() -> None:
    spec_a, records, _ = _example_cdr_a()
    # The same mapping under a second preset key: a second READING of the same file.
    spec_b = replace(spec_a, meta=replace(spec_a.meta, id="cdr_alt"))
    with RunStore() as store:
        source_id = _seed(store, records, presets=(spec_a, spec_b))
        shared_ordinals: dict[str, int] = {}
        for spec in (spec_a, spec_b):
            assemble_source(records, spec, store, source_id=source_id,
                            fingerprint_sha256=_FP, activity_ordinals=shared_ordinals)

        assert _one(store, "SELECT COUNT(*) AS n FROM source")["n"] == 1
        assert _one(store, "SELECT COUNT(*) AS n FROM raw_record")["n"] == 1
        assertions = _rows(store, "SELECT * FROM assertion")
        assert len(assertions) == 2
        assert len({a["preset_id"] for a in assertions}) == 2
        assert len({a["raw_record_id"] for a in assertions}) == 1   # shared raw_records
        # Two readings, two activities (record-scoped ids, continued ordinals).
        assert _one(store, "SELECT COUNT(*) AS n FROM activity")["n"] == 2
        # Same claim through two presets: claim_digest converges (preset-free), while
        # content_hash stays distinct (the preset key is in its prefix — decision 27).
        assert len({a["claim_digest"] for a in assertions}) == 1
        assert len({a["content_hash"] for a in assertions}) == 2


def test_two_presets_without_shared_ordinals_collide_loudly() -> None:
    spec_a, records, _ = _example_cdr_a()
    spec_b = replace(spec_a, meta=replace(spec_a.meta, id="cdr_alt"))
    with RunStore() as store:
        source_id = _seed(store, records, presets=(spec_a, spec_b))
        assemble_source(records, spec_a, store, source_id=source_id, fingerprint_sha256=_FP)
        # No shared registry: the second preset re-derives activity ordinal 0 for the
        # same record — a PK collision that must RAISE (no silent loss), documenting why
        # the caller shares one activity_ordinals dict across a run.
        with pytest.raises(sqlite3.IntegrityError):
            assemble_source(records, spec_b, store, source_id=source_id,
                            fingerprint_sha256=_FP)


# --- digests via core/ids -------------------------------------------------------

def _recompute_digests(store: RunStore, assertion: dict) -> tuple[str, str]:
    """Rebuild the L1 projection input from the STORED rows and re-derive both digests
    through core.ids — the round-trip proof that assemble used the normative recipe."""
    entity_row = _one(store, "SELECT * FROM entity WHERE entity_id = ?", assertion["entity_id"])
    entity = ids.EntityProjection(entity_row["identifier"], entity_row["identifier_type"],
                                  entity_row["type"])

    spatial = None
    if assertion["spatial_expression_id"] is not None:
        sx = _one(store, "SELECT * FROM spatial_expression WHERE spatial_expression_id = ?",
                  assertion["spatial_expression_id"])
        comps = _rows(store, "SELECT * FROM spatial_component WHERE spatial_expression_id = ? "
                             "ORDER BY ordinal", assertion["spatial_expression_id"])
        spatial = ids.SpatialProjection(
            sx["referential"], sx["spatial_source"],
            tuple(ids.SpatialComponentProjection(c["part"], c["spatial_role"], c["raw_value"])
                  for c in comps))

    temporal = None
    if assertion["temporal_expression_id"] is not None:
        tx = _one(store, "SELECT * FROM temporal_expression WHERE temporal_expression_id = ?",
                  assertion["temporal_expression_id"])
        comps = _rows(store, "SELECT * FROM temporal_component WHERE temporal_expression_id = ? "
                             "ORDER BY ordinal", assertion["temporal_expression_id"])
        temporal = ids.TemporalProjection(
            tx["kind"], tx["referential"],
            tuple(ids.TemporalComponentProjection(c["part"], c["temporal_role"],
                                                  c["stated_offset"], c["raw_value"])
                  for c in comps))

    activity = None
    if assertion["activity_id"] is not None:
        act = _one(store, "SELECT * FROM activity WHERE activity_id = ?",
                   assertion["activity_id"])
        context = json.loads(act["context_json"]) if act["context_json"] is not None else None
        activity = ids.ActivityProjection(act["kind"], context)

    links = ids.AssertionLinks(assertion["entity_role"], assertion["entity_position_link"],
                               assertion["entity_time_link"], assertion["spatial_temporal_link"])
    l1 = ids.l1_projection(entity=entity, spatial=spatial, temporal=temporal, links=links,
                           deleted=assertion["deleted"], multiplicity=assertion["multiplicity"],
                           activity=activity)
    provenance = _one(store,
                      "SELECT s.fingerprint_sha256 AS fp, r.line_no AS line_no FROM raw_record r "
                      "JOIN source s ON s.source_id = r.source_id WHERE r.raw_record_id = ?",
                      assertion["raw_record_id"])
    preset_key = _one(store, "SELECT key FROM preset WHERE preset_id = ?",
                      assertion["preset_id"])["key"]
    return ids.digests(fingerprint_sha256=provenance["fp"], preset_key=preset_key,
                       line_no=provenance["line_no"],
                       output_ordinal=assertion["output_ordinal"], l1=l1)


@pytest.mark.parametrize("name", ["cdr_a", "pin", "route", "cache"])
def test_stored_digests_recompute_via_core_ids(name: str) -> None:
    spec, records, _ = _EXAMPLES[name]()
    with _assembled(spec, records) as (store, _, _):
        for assertion in _rows(store, "SELECT * FROM assertion"):
            content_hash, claim_digest = _recompute_digests(store, assertion)
            assert content_hash == assertion["content_hash"]
            assert claim_digest == assertion["claim_digest"]


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
    source_ids: list[str] = []
    for input_path in (folder, zip_path):
        with RunStore() as store:
            ingest_input(input_path, store, case_number="C1", presets=(_preset_row(spec),))
            source = _one(store, "SELECT source_id, fingerprint_sha256 FROM source")
            records = [RawRecord(line_no=r["line_no"], fields=json.loads(r["raw_json"]))
                       for r in _rows(store, "SELECT line_no, raw_json FROM raw_record "
                                             "ORDER BY line_no")]
            assemble_source(records, spec, store, source_id=source["source_id"],
                            fingerprint_sha256=source["fingerprint_sha256"])
            digest_sets.append({(a["content_hash"], a["claim_digest"])
                                for a in _rows(store, "SELECT * FROM assertion")})
            source_ids.append(source["source_id"])

    # Two disclosures, two sources — but no path reaches a digest, so both sets match.
    assert source_ids[0] != source_ids[1]
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
        assertion = _one(store, "SELECT * FROM assertion")
        assert assertion["source_label"] == "N/A"      # evaluated, produced nothing
        assert assertion["deleted"] is None            # never mapped -> NULL


def test_operator_entity_param_overrides_and_sets_origin() -> None:
    spec, records, _ = _example_cache()
    with _assembled(spec, records, entity="+41791234567") as (store, _, _):
        entity = _one(store, "SELECT * FROM entity")
        assert entity["raw_identifier"] == "+41791234567"
        assert _one(store, "SELECT entity_origin FROM assertion")["entity_origin"] \
            == "operator_param"                        # R24: operator input is labelled


def test_data_source_reported_at_source_grain_not_written() -> None:
    spec, records, _ = _example_cache()
    spec = replace(spec, common=(*spec.common,
                                 _f("data_source", "const(/private/var/mobile/Cache.sqlite)")))
    with _assembled(spec, records) as (store, report, _):
        # decision 24: assemble RESOLVES the source-grain value and reports it; the
        # source row stays the L0 writer's to fill — assemble never updates it.
        assert report.data_source == "/private/var/mobile/Cache.sqlite"
        assert _one(store, "SELECT data_source FROM source")["data_source"] is None


def test_multiplicity_stored_and_null_means_one() -> None:
    spec, records, _ = _example_cctv()
    spec = replace(spec, common=(*spec.common, _f("multiplicity", "const(3)")))
    with _assembled(spec, records) as (store, _, _):
        assert _one(store, "SELECT multiplicity FROM assertion")["multiplicity"] == 3
    spec_plain, records, _ = _example_cctv()
    with _assembled(spec_plain, records) as (store, _, _):
        # R15: an unmapped multiplicity is NULL — which reads as 1, nothing invented.
        assert _one(store, "SELECT multiplicity FROM assertion")["multiplicity"] is None


# --- FIX-11: writer-correctness / silent-loss fixes ---------------------------

def test_common_mapped_coordinate_produces_a_spatial_corner() -> None:
    # FIX-11 #2: latitude/longitude mapped under common: must emit a spatial COMPONENT shared
    # by the assertion, not vanish into `shared` — otherwise the position and its digest are lost.
    spec = _preset("common_coords", common=[
        _f("entity_type", "const(device)"), _f("entity_role", "const(subject)"),
        _f("latitude", "field(lat)"), _f("longitude", "field(lon)"),
        _f("spatial_referential", "const(epsg:4326)"),
    ], assertions=[AssertionTemplate(
        fields=tuple(_links("at", "observed_at", "instant")),
        temporal=(_time("instant", "field(ts)", epoch="unix_s"),),
    )])
    records = _records({"lat": "46.2", "lon": "6.1", "ts": "1773450680"})
    with _assembled(spec, records) as (store, _, _):
        comps = _rows(store, "SELECT * FROM spatial_component ORDER BY ordinal")
        assert [(c["part"], c["source_field"], c["raw_value"]) for c in comps] == [
            ("latitude", "lat", "46.2"), ("longitude", "lon", "6.1")]
        sx = _one(store, "SELECT * FROM spatial_expression")
        assert sx["referential"] == "epsg:4326"          # from the explicit common spatial_referential
        assertion = _one(store, "SELECT * FROM assertion")
        assert assertion["spatial_expression_id"] is not None
        assert assertion["claim_digest"] is not None     # a real placed assertion, not placeless


def test_resolver_picks_up_a_field_present_only_in_a_later_record() -> None:
    # FIX-11 #3: a column absent from record[0] but present in a LATER record must still resolve
    # (heterogeneous JSON/JSONL) — seeding the resolver from record[0] alone would drop it.
    spec = _preset("hetero", common=[
        _f("entity", "field(imei)"), _f("identifier_type", "const(msisdn)"),
        _f("entity_type", "const(device)"), _f("entity_role", "const(subject)"),
    ], assertions=[AssertionTemplate(
        fields=tuple(_links("references", "event_at", "instant")),
        temporal=(_time("instant", "field(ts)", epoch="unix_s"),),
    )])
    # 'imei' appears ONLY in the second record.
    records = _records({"ts": "1773450680"},
                       {"imei": "352099001761481", "ts": "1773450681"})
    with _assembled(spec, records) as (store, _, _):
        idents = [row["raw_identifier"] for row in _rows(
            store,
            "SELECT e.raw_identifier FROM entity e "
            "JOIN assertion a ON a.entity_id = e.entity_id "
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
        entity = _one(store, "SELECT * FROM entity")
        assert entity["raw_identifier"] == ""            # the operator's blank, not the preset value
        assert _one(store, "SELECT entity_origin FROM assertion")["entity_origin"] \
            == "operator_param"


# --- FIX-02: per-bound accuracy component guard -------------------------------

def _acc_interval_spec_records(with_lower_acc: bool):
    """An interval preset whose LOWER bound is MISSING (no 'start') but UPPER present, with the
    upper accuracy mapped (+ the lower accuracy too when ``with_lower_acc``)."""
    fields = [_f("time_accuracy_us_upper", "field(acc_hi)")]
    if with_lower_acc:
        fields.insert(0, _f("time_accuracy_us_lower", "field(acc_lo)"))
    spec = _preset("acc_interval", common=[
        _f("entity_type", "const(device)"), _f("entity_role", "const(subject)"),
    ], assertions=[AssertionTemplate(
        fields=tuple([*fields, *_links("at", "observed_at", "continuous_during_interval")]),
        temporal=(_time("interval", "field(start)", "field(end)", epoch="unix_s"),),
    )])
    records = _records({"end": "1773450681", "acc_lo": "500000", "acc_hi": "1000000"})
    return spec, records


def test_malformed_interval_gates_accuracy_per_side() -> None:
    # FIX-02: lower bound missing, upper present, BOTH accuracies mapped. The lower accuracy must
    # NOT be written as an orphan (no lower bound); the upper accuracy IS written; and the
    # claim_digest equals the same record WITHOUT the stray lower-accuracy mapping.
    spec, records = _acc_interval_spec_records(with_lower_acc=True)
    with _assembled(spec, records) as (store, _, _):
        parts = [(c["part"], c["raw_value"])
                 for c in _rows(store, "SELECT * FROM temporal_component ORDER BY ordinal")]
        assert ("upper", "1773450681") in parts
        assert ("upper_accuracy", "1000000") in parts
        assert not any(part == "lower_accuracy" for part, _ in parts)   # no orphan
        digest_with = _one(store, "SELECT claim_digest FROM assertion")["claim_digest"]

    spec2, records2 = _acc_interval_spec_records(with_lower_acc=False)
    with _assembled(spec2, records2) as (store, _, _):
        digest_without = _one(store, "SELECT claim_digest FROM assertion")["claim_digest"]
    # The malformed lower accuracy contributed nothing to identity, as it must not.
    assert digest_with == digest_without


# --- datum → referential: the L1 angle parts are datum-agnostic ---------------

def _agnostic_coords_preset(key: str, *, with_referential: bool) -> PresetSpec:
    """A preset mapping the datum-agnostic ``latitude``/``longitude`` parts, optionally with
    an explicit ``spatial_referential: const(epsg:4326)`` — the datum carrier."""
    position = [_f("latitude", "field(lat)"), _f("longitude", "field(lon)")]
    if with_referential:
        position.append(_f("spatial_referential", "const(epsg:4326)"))
    return _preset(key, common=[
        _f("entity_type", "const(device)"), _f("entity_role", "const(subject)"),
    ], assertions=[AssertionTemplate(
        fields=tuple([*position, *_links("at", "observed_at", "instant")]),
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
