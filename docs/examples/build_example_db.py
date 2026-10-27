#!/usr/bin/env python3
"""Deterministic worked-example run database for the settled 21-table model.

WHAT THIS FILE IS
-----------------
A hand-written **demonstration artefact**. It builds
``docs/examples/matlas_example.sqlite`` directly from the schema
([../model/schema.md](../model/schema.md)) so the model can be exercised with realistic
data. The DDL, the digest recipes and the ingest order are transcribed by hand here as a
readable reference; the shipped schema is generated from the code.

USED BY
-------
- ``docs/examples/example_queries.md`` — regenerated from ``--queries`` output.
- ``docs/model_worked_examples.md`` — every row-by-layer walk is spot-checked against
  the rows this builder writes.

DEPENDS ON
----------
- the settled schema doc (transcribed below) and ``s2sphere`` (the S2 leaf cell
  per spatial_integration; pure-Python,).

DETERMINISM (the whole point)
-----------------------------
Every id is ``uuid5`` over natural-key parts joined with the unit separator ``\\x1f``
(D-"id scheme"; a ``|`` in a path could otherwise forge equal keys). The *only*
non-deterministic roots are ``case`` and ``run`` (uuid4 — the run scope);
no EVIDENCE id derives from them, so running the builder twice yields byte-identical
DBs apart from those two rows and the run-scoped metadata that carries the run id
(``run_archive``/``run_preset`` links, ``source.run_id``, ``edit_log``). Floats are captured as **source text** (never re-parsed) so the
canonical-JSON rule (the canonical-JSON rule, no float re-formatting) holds and the
digests stay stable.

Run:  python3 build_example_db.py            # (re)build the DB
      python3 build_example_db.py --queries  # build + print the demo requests table
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import s2sphere

DB = Path(__file__).parent / "matlas_example.sqlite"
CSV = Path(__file__).parent / "matlas_example.csv"

# Sentinel for a spatial expression the engine CANNOT integrate today. The demo used to
# fabricate ``cell_register/…``, ``camera_register/…``, ``geocode_nominatim/…`` and
# ``place_centroid/…`` rows with invented coordinates, but every one of those methods is a
# ``_NotAvailableMethod`` stub in ``integrate/spatial.py`` (``camera_register`` has no class
# at all). A fixture that shows a derivation the engine cannot perform teaches the reader a
# capability that does not exist — and it misled a reader of this very file. The L1
# expression stays, because a cell reference or an address IS evidence; only the integration
# is withheld, which is exactly what a real run produces.
_UNAVAILABLE = None

# One namespace per id space keeps two logically distinct rows that happen to share
# natural-key text from ever colliding (schema §"The id scheme": one namespace per table).
NS = uuid.UUID("9c4e7b1a-2d63-5f48-8a17-3b5c6d0e9f21")
SEP = "\x1f"  # unit separator — the join delimiter
_ESC = "\x1b"  # escape introducer: _txt escapes _ESC and SEP so a value can never inject
               # a bare delimiter (keeps l1_projection injective — schema §"L1 projection")

# Optional-corner presence flags (schema §"The normative L1 projection"): an absent corner
# and a present-but-empty one must render differently, so each optional section leads with
# one of these; a present spatial/temporal corner also carries its component COUNT.
_CORNER_PRESENT = "+"
_CORNER_ABSENT = "-"

# S2 leaf level: CellId.from_lat_lng always returns a level-30 leaf cell.
S2_LEAF_LEVEL = 30


def new_id() -> str:
    """A fresh uuid4 for one EVIDENCE-CHAIN row — mirrors ``ids.new_id`` (decision 50).

    CONSEQUENCE for this fixture: the built database is NO LONGER byte-reproducible. Every
    rebuild produces different ids, so its diff is not a review artefact any more; compare
    the CSV's content columns, or the digests, which stay content-derived.
    """
    return str(uuid.uuid4())


def u5(table: str, *parts: object) -> str:
    """uuid5 over (table-tag ⟂ natural-key parts). Deterministic across runs."""
    joined = SEP.join(table if i == 0 else _txt(p) for i, p in enumerate((table, *parts)))
    return str(uuid.uuid5(NS, joined))


def _txt(part: object) -> str:
    # None becomes the empty string so a missing part is distinct-but-stable, never a
    # literal "None" that could clash with a real value. Then escape the two reserved
    # control bytes (introducer first, then SEP) so a value carrying a literal 0x1F can
    # never inject a delimiter — this is what keeps the join / L1 projection injective.
    if part is None:
        return ""
    return str(part).replace(_ESC, _ESC + _ESC).replace(SEP, _ESC + SEP)


def s2_signed_leaf(lat: float, lon: float) -> int:
    """Signed-int64 S2 leaf cell id for (lat, lon).

    HOW: s2sphere returns an *unsigned* 64-bit cell id; SQLite INTEGER is signed, so we
    reinterpret the top-bit-set values as negative (v - 2**64 when v >= 2**63). WHY:
    storing the same 64 bits SQLite will compare and range-scan on (which relies on
    ``WHERE s2_cell_id BETWEEN ? AND ?``); a naive unsigned store would overflow.
    """
    cell = s2sphere.CellId.from_lat_lng(s2sphere.LatLng.from_degrees(lat, lon))
    v = cell.id()
    return v - 2**64 if v >= 2**63 else v


# --------------------------------------------------------------------------- #
# Canonical JSON + the normative L1 projection (the digest recipes)           #
# --------------------------------------------------------------------------- #

def canonical_json(obj: object) -> str:
    """Canonical JSON per the canonical-JSON rule: sorted keys, no insignificant
    whitespace. Float source-text is preserved by keeping numbers as strings upstream,
    so json.dumps never re-formats a value it was given."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def l1_projection(*, entity, bearer, spatial, temporal) -> str:
    """The NORMATIVE L1 projection both digests hash (schema §"The normative L1
    projection"), in the exact field order — and only those fields.

    Every variable-length section is FRAMED so the projection is injective: each optional
    corner leads with a presence flag, and a present spatial/temporal corner carries an
    explicit component COUNT before its rows (so a component can never shift across the
    spatial/temporal boundary, and an absent corner never renders like a present-but-empty
    one). Combined with ``_txt`` escaping SEP, the token stream is uniquely recoverable.

    Excluded normatively: concerns / concerns_uid / both origins, raw_identifier,
    component source_fields, all paths, all ids/run_id, all integrations, all analysis —
    and the LINKS (decision 55): they are the preset author's statement, not record
    content, so two readings disagreeing on a link still converge. A `subject` row passes
    no bearer: its bearer IS the targeted entity, already in the first block.
    Those are deliberately absent below so a register/tz upgrade, an operator param, or
    a renamed source column can never move an evidence digest.
    """
    parts: list[str] = []

    # targeted entity: identifier ⟂ identifier_type ⟂ type (always 3 slots)
    parts += [_txt(entity["identifier"]), _txt(entity["identifier_type"]),
              _txt(entity["type"])]
    # bearer: presence ⟂ [identifier ⟂ identifier_type ⟂ type]. The flag is what keeps an
    # ABSENT bearer distinguishable from a present one whose fields are all empty.
    if bearer is None:
        parts += [_CORNER_ABSENT]
    else:
        parts += [_CORNER_PRESENT, _txt(bearer["identifier"]),
                  _txt(bearer["identifier_type"]), _txt(bearer["type"])]

    # spatial: presence ⟂ [referential ⟂ spatial_source ⟂ count ⟂ components(part,role,raw)]
    if spatial is None:
        parts += [_CORNER_ABSENT]
    else:
        parts += [_CORNER_PRESENT, _txt(spatial["referential"]),
                  _txt(spatial["spatial_source"]), str(len(spatial["components"]))]
        for c in spatial["components"]:  # already ordinal-ordered
            parts += [_txt(c["part"]), _txt(c["unit"]), _txt(c["raw_value"])]

    # temporal: presence ⟂ [kind ⟂ referential ⟂ count ⟂ components(part,role,offset,raw)]
    if temporal is None:
        parts += [_CORNER_ABSENT]
    else:
        parts += [_CORNER_PRESENT, _txt(temporal["kind"]),
                  _txt(temporal["referential"]), str(len(temporal["components"]))]
        for c in temporal["components"]:
            parts += [_txt(c["part"]),
                      _txt(c["stated_offset"]), _txt(c["raw_value"])]

    # recovery_state / recovery_source are EXCLUDED like paths: HOW a record was obtained is
    # provenance, not identity, so a claim carved from free space converges with the same
    # claim read live (decision 48).
    return SEP.join(parts)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Temporal helpers                                                            #
# --------------------------------------------------------------------------- #

def us_from_iso(iso_with_offset: str) -> int:
    """'YYYY-MM-DD HH:MM:SS+HH' -> unix microseconds. Whole-hour offsets only (demo)."""
    base, off = iso_with_offset[:-3], int(iso_with_offset[-3:])
    dt = datetime.strptime(base, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone(timedelta(hours=off)))
    return int(dt.timestamp() * 1_000_000)


DEMO_LOCAL_ZONE = "Europe/Paris"
"""The case's configured local zone (``matlas_config.toml`` -> ``timezone.local_zone``).

The demo hard-codes one because ``offset_to_local_hours_*`` is a property of the CASE, not
of any source: every row gets the offset an analyst adds to the canonical UTC µs to read
that instant in this zone.

That every demo row lands on ``+1.0`` is the point, not a flaw. The sources here are a mix
of epochs, offset-bearing clocks and naive ones, and under the OLD applied-offset meaning
this column disagreed row by row (and was NULL for every epoch) while telling the analyst
nothing about the zone they actually read in. It is uniform now because the demo's instants
all fall in March, before the European DST change; a case spanning it would show +1 and +2,
which is why the value is stored per bound rather than once per run.
"""


def offset_to_local(unix_us: int | None, zone: str | None = DEMO_LOCAL_ZONE):
    """Hours to ADD to ``unix_us`` to read it in ``zone`` — mirrors
    ``model_atlas.integrate.temporal.offset_to_local``.

    None when there is no instant to offset; the loud "N/A" when no zone is configured;
    otherwise the zone's offset AT that instant, so DST is resolved per bound.
    NOTE this is the UTC -> local direction. It is NOT the offset the source's clock
    carried: that survives in the verbatim raw value and its column name, and HOW the
    instant was reached is stamped in ``method``.
    """
    if unix_us is None:
        return None
    if not zone:
        return "N/A"
    instant = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=unix_us)
    return instant.astimezone(ZoneInfo(zone)).utcoffset().total_seconds() / 3600


# --------------------------------------------------------------------------- #
# DDL — the 19 tables + matlas_meta, transcribed from the schema (docs/model/schema.md)        #
# --------------------------------------------------------------------------- #

DDL = """
PRAGMA foreign_keys = ON;

-- Layer 0 ------------------------------------------------------------------
CREATE TABLE case_metadata (case_id TEXT PRIMARY KEY, case_number TEXT NOT NULL,
    title TEXT, description TEXT, created_at TEXT, created_by TEXT);
CREATE TABLE run (run_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES case_metadata(case_id),
    input_path TEXT, started_at TEXT, finished_at TEXT, tool_version TEXT,
    settings_json TEXT, examiner TEXT);
CREATE TABLE matlas_meta (schema_version TEXT, families_version TEXT,
    normalisation_ruleset TEXT, stemming_ruleset TEXT);
CREATE TABLE archive (archive_id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE, cd_sha256 TEXT NOT NULL);
CREATE INDEX ix_archive_cd ON archive(cd_sha256);
CREATE TABLE run_archive (run_archive_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES run(run_id),
    archive_id TEXT NOT NULL REFERENCES archive(archive_id),
    parsed_at TEXT, skipped INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, archive_id));
CREATE TABLE source_file (source_file_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES run(run_id),
    input_file_path TEXT NOT NULL, inner_path TEXT, reported_data_source TEXT,
    format TEXT, subunit TEXT, fingerprint_sha256 TEXT,
    sibling_fingerprints_json TEXT, exhibit_ref TEXT, source_tier TEXT,
    record_count INTEGER,
    UNIQUE (run_id, input_file_path, inner_path, subunit));
CREATE INDEX ix_source_fp ON source_file(fingerprint_sha256);
CREATE TABLE preset (preset_id TEXT PRIMARY KEY,
    key TEXT NOT NULL, name TEXT, version TEXT,
    definition_digest TEXT NOT NULL, UNIQUE (key, version));
CREATE TABLE run_preset (run_preset_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES run(run_id),
    preset_id TEXT NOT NULL REFERENCES preset(preset_id),
    UNIQUE (run_id, preset_id));
CREATE TABLE raw_record (raw_record_id TEXT PRIMARY KEY,
    source_file_id TEXT NOT NULL REFERENCES source_file(source_file_id), line_no INTEGER NOT NULL,
    native_record_uid TEXT, recovery_state TEXT, recovery_source TEXT,
    raw_json TEXT NOT NULL, UNIQUE (source_file_id, line_no));

-- Layer 1 ------------------------------------------------------------------
-- Kept but EMPTY: nothing writes activity (decision 51); observation.activity_id stays
-- NULL so the link exists when activity is finally modelled.
CREATE TABLE activity (activity_id TEXT PRIMARY KEY, kind TEXT);
CREATE TABLE entity (entity_id TEXT PRIMARY KEY,
    identifier TEXT, raw_identifier TEXT, identifier_type TEXT NOT NULL,
    type TEXT NOT NULL, concerns TEXT, concerns_origin TEXT, concerns_uid TEXT);
CREATE INDEX ix_entity_norm ON entity(identifier_type, identifier);
CREATE INDEX ix_entity_concerns ON entity(concerns_uid);
CREATE TABLE spatial_expression (spatial_expression_id TEXT PRIMARY KEY,
    referential TEXT, position_type TEXT NOT NULL, spatial_source TEXT);
CREATE TABLE spatial_component (spatial_component_id TEXT PRIMARY KEY,
    spatial_expression_id TEXT NOT NULL
        REFERENCES spatial_expression(spatial_expression_id),
    ordinal INTEGER NOT NULL, part TEXT NOT NULL,
    source_field TEXT, raw_value TEXT, unit TEXT,
    UNIQUE (spatial_expression_id, ordinal));
CREATE TABLE temporal_expression (temporal_expression_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL, referential TEXT, temporal_source TEXT);
CREATE TABLE temporal_component (temporal_component_id TEXT PRIMARY KEY,
    temporal_expression_id TEXT NOT NULL
        REFERENCES temporal_expression(temporal_expression_id),
    ordinal INTEGER NOT NULL, part TEXT NOT NULL,
    stated_offset TEXT, source_field TEXT, raw_value TEXT,
    UNIQUE (temporal_expression_id, ordinal));
CREATE TABLE observation (observation_id TEXT PRIMARY KEY,
    raw_record_id TEXT NOT NULL REFERENCES raw_record(raw_record_id),
    preset_id TEXT NOT NULL REFERENCES preset(preset_id),
    entity_id TEXT NOT NULL UNIQUE REFERENCES entity(entity_id),
    activity_id TEXT REFERENCES activity(activity_id),
    spatial_expression_id TEXT UNIQUE
        REFERENCES spatial_expression(spatial_expression_id),
    temporal_expression_id TEXT UNIQUE
        REFERENCES temporal_expression(temporal_expression_id),
    bearer_entity_id TEXT UNIQUE REFERENCES entity(entity_id),
    entity_link TEXT, spatial_link TEXT, temporal_link TEXT,
    recovery_state TEXT, recovery_source TEXT,
    source_label TEXT, input_record_id TEXT, source_record_uid TEXT,
    -- No UNIQUE over (raw_record_id, preset_id): one preset with several observation
    -- templates writes several rows for one record, and output_ordinal (which used to
    -- discriminate them) went with decision 50.
    observation_content_hash TEXT NOT NULL, spatiotemporal_entity_hash TEXT NOT NULL,
    -- Decision 55's link/bearer invariants, as in the real schema (IS = NULL-safe).
    CHECK ((entity_link IS 'subject') = (bearer_entity_id IS entity_id)),
    CHECK (entity_link IS NOT 'party' OR (bearer_entity_id IS NOT NULL
           AND bearer_entity_id IS NOT entity_id)),
    CHECK (entity_link IS NOT 'unknown' OR bearer_entity_id IS NULL),
    CHECK (spatial_expression_id IS NOT NULL OR spatial_link IS NULL));
CREATE INDEX ix_observation_ch ON observation(observation_content_hash);
CREATE INDEX ix_observation_cd ON observation(spatiotemporal_entity_hash);
CREATE INDEX ix_observation_act ON observation(activity_id);

-- Layer 2 ------------------------------------------------------------------
CREATE TABLE spatial_integration (spatial_integration_id TEXT PRIMARY KEY,
    spatial_expression_id TEXT NOT NULL UNIQUE
        REFERENCES spatial_expression(spatial_expression_id),
    method TEXT NOT NULL, position_raw TEXT, referential TEXT, spatial_source TEXT,
    latitude_wgs84 REAL, longitude_wgs84 REAL, altitude_m REAL,
    horizontal_accuracy_m REAL, vertical_accuracy_m REAL,
    horizontal_speed_kmh REAL, vertical_speed_kmh REAL,
    horizontal_speed_accuracy_kmh REAL, vertical_speed_accuracy_kmh REAL,
    heading_deg REAL, heading_accuracy_deg REAL,
    beam_azimuth_deg REAL, beam_width_deg REAL);
CREATE TABLE spatial_index (spatial_index_id TEXT PRIMARY KEY,
    spatial_integration_id TEXT NOT NULL UNIQUE
        REFERENCES spatial_integration(spatial_integration_id),
    s2_cell_id INTEGER, s2_level INTEGER);
CREATE INDEX ix_spatial_index_cell ON spatial_index(s2_cell_id);
CREATE TABLE timespan_integration (timespan_integration_id TEXT PRIMARY KEY,
    temporal_expression_id TEXT NOT NULL UNIQUE
        REFERENCES temporal_expression(temporal_expression_id),
    method TEXT NOT NULL, kind TEXT, referential TEXT, temporal_source TEXT,
    lower_unix_utc_us INTEGER, upper_unix_utc_us INTEGER,
    offset_to_local_hours_lower TEXT, offset_to_local_hours_upper TEXT,
    lower_accuracy_us INTEGER, upper_accuracy_us INTEGER);

-- Layer 3 ------------------------------------------------------------------
CREATE TABLE analysis (analysis_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES observation(observation_id),
    method TEXT NOT NULL, record_rank INTEGER, record_rank_type TEXT,
    UNIQUE (observation_id, method));
CREATE TABLE edit_log (edit_id TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL UNIQUE,
    table_name TEXT, row_id TEXT, column TEXT, old_value TEXT, new_value TEXT,
    edit_kind TEXT, edited_by TEXT, edited_at TEXT);

-- Findings are VIEWS, never merges (the silent-loss guards).
CREATE VIEW duplicate_sources AS
    SELECT fingerprint_sha256, COUNT(*) AS copies,
           GROUP_CONCAT(input_file_path, ' | ') AS files
    FROM source_file GROUP BY fingerprint_sha256 HAVING COUNT(*) > 1;
CREATE VIEW possible_same_evidence AS
    SELECT a.spatiotemporal_entity_hash, COUNT(*) AS copies,
           GROUP_CONCAT(s.input_file_path, ' | ') AS files
    FROM observation a
    JOIN raw_record r ON r.raw_record_id = a.raw_record_id
    JOIN source_file s ON s.source_file_id = r.source_file_id
    GROUP BY a.spatiotemporal_entity_hash HAVING COUNT(*) > 1;
"""

# --------------------------------------------------------------------------- #
# Geography of the demo (Geneva, one night)                                   #
# --------------------------------------------------------------------------- #

SCENE = (46.2030, 6.1400)      # Rue du Stand 15, 1204 Geneve
HOME = (46.1930, 6.1290)
CAM_A7 = (46.2044, 6.1432)     # ANPR camera near the scene
CELL_A = (46.2050, 6.1380)     # operator-A tower next to the scene
CELL_B = (46.1902, 6.1265)     # operator-B tower (B-leg)
STATION = (46.2102, 6.1424)    # Cornavin


class Builder:
    """Thin insert wrapper so the worked example below reads as a narrative.

    Each method returns the surrogate id of the row it wrote, honouring the schema's
    ingest write order: run -> source_file -> raw_record -> entity +
    expressions + components -> observation -> integrations -> analysis. No
    ``INSERT OR IGNORE`` anywhere: a UNIQUE violation here would be a derivation bug and
    must raise (the ingestion write order).
    """

    def __init__(self, cx: sqlite3.Connection, case_id: str, run_id: str):
        self.cx = cx
        self.case_id = case_id
        self.run_id = run_id
        self._presets: dict[tuple[str, str], str] = {}

    # ---- Layer 0 ---------------------------------------------------------
    def preset(self, key: str, name: str, version: str) -> str:
        # Content-keyed: a preset's definition IS its identity. The definition digest adds a
        # definition_digest beside the version to catch un-bumped edits; the real
        # pipeline hashes the canonical preset YAML — the demo hashes a stub.
        pid = u5("preset", key, version)
        if (key, version) not in self._presets:
            dd = hashlib.sha256(
                json.dumps({"key": key, "name": name, "version": version},
                           sort_keys=True).encode()).hexdigest()
            self.cx.execute("INSERT INTO preset VALUES (?,?,?,?,?)",
                            (pid, key, name, version, dd))
            # the run records every preset selected for it.
            self.cx.execute("INSERT INTO run_preset VALUES (?,?,?)",
                            (u5("run_preset", self.run_id, pid), self.run_id, pid))
            self._presets[(key, version)] = pid
        return pid

    def source(self, path, inner, data_source, fmt, subunit, fp, exhibit=None,
               tier="primary", siblings=None):
        # id from PATHS, never the fingerprint: two byte-identical exhibits
        # stay two sources whose matching hash is a finding, not a merge.
        sid = new_id()
        self.cx.execute("INSERT INTO source_file VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, self.run_id, path, inner, data_source, fmt, subunit, fp,
             json.dumps(siblings or []), exhibit, tier, None))
        return sid

    def archive(self, path, cd_sha256, skipped=0):
        # id from the path only (a content hash is never a key); the
        # run link is a row in run_archive (one run, many archives; one archive,
        # many runs), so every encounter - including an opt-in skip - is recorded.
        aid = u5("archive", path)
        self.cx.execute("INSERT INTO archive VALUES (?,?,?)", (aid, path, cd_sha256))
        self.cx.execute("INSERT INTO run_archive VALUES (?,?,?,?,?)",
            (u5("run_archive", self.run_id, aid), self.run_id, aid,
             "2026-03-15T08:00:05Z", skipped))
        return aid

    def record(self, sid, line, raw: dict, recovery="live", recovery_source=None):
        rrid = new_id()
        # raw_json is the COMPLETE row (verbatim values). recovery_source is the fine origin
        # (wal/journal/freelist/freespace) behind the coarse recovery_state, and stays NULL
        # for a live or intact row — recovered from nowhere, so a populated cell IS the
        # "this row was recovered" signal rather than an echo of recovery_state.
        self.cx.execute("INSERT INTO raw_record VALUES (?,?,?,?,?,?,?)",
            (rrid, sid, line, None, recovery, recovery_source, canonical_json(raw)))
        return rrid

    # ---- Layer 1 ---------------------------------------------------------
    def entity(self, rrid, ordinal, identifier, raw_identifier, id_type, etype,
               concerns=None, concerns_origin=None):
        eid = new_id()
        concerns_uid = None
        if concerns is not None:
            # case-folded/stemmed digest so "A. Moreau"/"moreau a." group together,
            # derived, never a key, excluded from the evidence digests.
            folded = " ".join(sorted(concerns.lower().replace(".", " ").split()))
            concerns_uid = sha256_hex(folded)[:16]
        self.cx.execute("INSERT INTO entity VALUES (?,?,?,?,?,?,?,?)",
            (eid, identifier, raw_identifier, id_type, etype,
             concerns, concerns_origin, concerns_uid))
        return eid, {"identifier": identifier, "identifier_type": id_type, "type": etype}

    def spatial(self, rrid, ordinal, position_type, referential, spatial_source,
                components, method, lat, lon, **metrics):
        """components: list of (part, unit, source_field, raw_value)."""
        sx = new_id()
        # position_type is STATED, and the referential belongs to the two
        # self-contained-transformation types only — a lookup type stores NULL.
        from model_atlas.core.vocabularies import PositionType
        kind = PositionType(position_type)
        self.cx.execute("INSERT INTO spatial_expression VALUES (?,?,?,?)",
            (sx, referential if kind.takes_referential else None,
             kind.value, spatial_source))
        comp_dicts = []
        for i, (part, unit, field, rawv) in enumerate(components):
            self.cx.execute("INSERT INTO spatial_component VALUES (?,?,?,?,?,?,?)",
                (new_id(), sx, i, part, field, rawv, unit))
            comp_dicts.append({"part": part, "unit": unit, "raw_value": rawv})
        # An integration row exists when the pass produced ANY canonical column — the
        # engine's own rule (``if not columns: return None`` in integrate/spatial.py).
        # A cell reference whose coordinates need an unavailable register STILL yields a
        # row if its beam metrics converted: the derived block is partial, not absent.
        # Only a place with nothing derivable at all gets no row, and no position is ever
        # invented to fill one.
        if lat is not None or any(v is not None for v in metrics.values()):
            si = new_id()
            # The verbatim components behind the two canonical coordinate columns —
            # exactly what the real integration records, so the demo shows the same shape.
            position_raw = " | ".join(
                f"{fld}:{raw}" for part, _u, fld, raw in components
                if part in ("latitude", "longitude") and raw is not None) or None
            self.cx.execute("INSERT INTO spatial_integration VALUES "
                            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (si, sx, method, position_raw, referential, spatial_source, lat, lon,
                 metrics.get("altitude_m"), metrics.get("horizontal_accuracy_m"),
                 metrics.get("vertical_accuracy_m"), metrics.get("horizontal_speed_kmh"),
                 metrics.get("vertical_speed_kmh"),
                 metrics.get("horizontal_speed_accuracy_kmh"),
                 metrics.get("vertical_speed_accuracy_kmh"),
                 metrics.get("heading_deg"), metrics.get("heading_accuracy_deg"),
                 metrics.get("beam_azimuth_deg"), metrics.get("beam_width_deg")))
            if lat is not None:
                # one S2 leaf cell per POINT: a metrics-only row is not a point and is
                # deliberately unindexed (integrate/spatial.py makes the same distinction).
                self.cx.execute("INSERT INTO spatial_index VALUES (?,?,?,?)",
                    (new_id(), si, s2_signed_leaf(lat, lon), S2_LEAF_LEVEL))
        # ordinal ordering matters for the L1 projection, so hand it back ready-sorted.
        return sx, {"referential": referential, "spatial_source": spatial_source,
                    "components": comp_dicts}

    def temporal(self, rrid, ordinal, kind, referential, temporal_source, components,
                 method, lower_us, upper_us,
                 lower_accuracy_us=None, upper_accuracy_us=None, local_zone=DEMO_LOCAL_ZONE):
        """components: list of (part, stated_offset, source_field, raw)."""
        tx = new_id()
        self.cx.execute("INSERT INTO temporal_expression VALUES (?,?,?,?)",
            (tx, kind, referential, temporal_source))
        comp_dicts = []
        for i, (part, stated, field, rawv) in enumerate(components):
            self.cx.execute("INSERT INTO temporal_component VALUES (?,?,?,?,?,?,?)",
                (new_id(), tx, i, part, stated, field, rawv))
            comp_dicts.append({"part": part, "stated_offset": stated, "raw_value": rawv})
        # The offsets are DERIVED from the instants + the case's zone, never passed in per
        # call: production stamps them in one funnel (integrate_expression) for exactly this
        # reason — a per-call-site offset is a per-call-site chance to state a wrong one.
        self.cx.execute("INSERT INTO timespan_integration VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id(), tx, method, kind, referential, temporal_source,
             lower_us, upper_us,
             _off_text(offset_to_local(lower_us, local_zone)),
             _off_text(offset_to_local(upper_us, local_zone)),
             lower_accuracy_us, upper_accuracy_us))
        return tx, {"kind": kind, "referential": referential, "components": comp_dicts}

    def observation(self, rrid, preset_id, preset_key, ordinal, entity_pair,
                  spatial_pair, temporal_pair,
                  entity_link, spatial_link, temporal_link,
                  bearer_pair=None,
                  recovery_state=None, recovery_source=None,
                  label=None, input_record_id=None, source_record_uid=None):
        eid, entity_l1 = entity_pair
        # A `subject` holds its own position: its bearer IS the targeted entity (decision 55),
        # and the projection carries no separate bearer block for it.
        bearer_id, bearer_l1 = ((eid, None) if entity_link == "subject"
                                else bearer_pair or (None, None))
        sx, spatial_l1 = spatial_pair or (None, None)
        tx, temporal_l1 = temporal_pair or (None, None)
        l1 = l1_projection(entity=entity_l1, bearer=bearer_l1, spatial=spatial_l1,
                           temporal=temporal_l1)
        # spatiotemporal_entity_hash = the L1 projection ALONE — preset/file-independent, so two
        # presets extracting the same claim converge and surface as possible-same-evidence.
        spatiotemporal_entity_hash = sha256_hex(l1)
        fp, line_no = self.cx.execute(
            "SELECT s.fingerprint_sha256, r.line_no FROM raw_record r "
            "JOIN source_file s ON s.source_file_id=r.source_file_id WHERE r.raw_record_id=?",
            (rrid,)).fetchone()
        # observation_content_hash = fingerprint ⟂ preset key ⟂ line_no ⟂ L1 —
        # the digest scoped to THIS preset's reading of THIS file; no paths, so
        # folder==zip parity holds.
        observation_content_hash = sha256_hex(SEP.join(
            [_txt(fp), preset_key, str(line_no), l1]))
        aid = new_id()
        self.cx.execute("INSERT INTO observation VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, rrid, preset_id, eid, None, sx, tx,
             bearer_id, entity_link, spatial_link, temporal_link,
             recovery_state, recovery_source, label, input_record_id,
             source_record_uid, observation_content_hash, spatiotemporal_entity_hash))
        return aid

    def analysis(self, aid, method, record_rank):
        # The label is written from the SAME value as the rank, so the two cannot disagree
        # at the point of writing (decision 54). NULL rank -> NULL label.
        rank_type = (None if record_rank is None
                     else "main" if record_rank == 1 else "additional")
        self.cx.execute("INSERT INTO analysis VALUES (?,?,?,?,?)",
            (new_id(), aid, method, record_rank, rank_type))


def _off_text(off: float | str | None) -> str | None:
    # Applied offset per bound: numeric hours as text, or the "N/A" assumed-UTC sentinel,
    # or NULL when there is no bound at all.
    if off is None:
        return None
    return off if isinstance(off, str) else f"{float(off):+.1f}"


def build() -> sqlite3.Connection:
    if DB.exists():
        DB.unlink()
    cx = sqlite3.connect(DB)
    cx.executescript(DDL)

    # case + run: the only uuid4 rows; everything downstream is uuid5.
    case_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    cx.execute("INSERT INTO case_metadata VALUES (?,?,?,?,?,?)",
        (case_id, "GE-2026-04412", "Rue du Stand robbery",
         "One-night Geneva investigation — worked-example fixture", "2026-03-15T08:00:00Z",
         "J. Dupont"))
    cx.execute("INSERT INTO run VALUES (?,?,?,?,?,?,?,?)",
        (run_id, case_id, "/case", "2026-03-15T08:00:00Z", "2026-03-15T08:04:12Z",
         "matlas/example-0", "{}", "insp. Girard"))
    cx.execute("INSERT INTO matlas_meta VALUES (?,?,?,?)",
        ("example-0", "example-0", "norm_rules/1.0", "stem_rules/1.0"))

    b = Builder(cx, case_id, run_id)

    # Presets (companion table; one per reading applied in this run).
    p_cache = b.preset("ios_cache", "iOS location cache", "1.0")
    p_visit = b.preset("ios_visits", "iOS visits", "1.0")
    p_cdr = b.preset("cdr_generic", "Generic CDR", "1.0")
    p_anpr = b.preset("anpr_ch", "Swiss ANPR", "1.0")
    p_dvr = b.preset("dvr_generic", "Generic DVR index", "1.0")
    p_police = b.preset("police_reg", "Police register", "1.0")
    p_audit = b.preset("audition", "Witness audition", "1.0")
    p_im = b.preset("im_generic", "Generic IM", "1.0")
    p_nav = b.preset("nav_generic", "Generic navigation", "1.0")

    # ---- iOS Cache.sqlite fixes, ingested TWICE (folder + zip) ------------
    # WHY twice: folder==zip must produce equal observation_content_hash/spatiotemporal_entity_hash (no paths in
    # the digests) yet remain two distinct sources — the duplicate-evidence finding.
    b.archive("/case/exhibit_A_copy.zip", "cd-exA-copy-abc123")
    fixes = [  # (line, lat, lon, acc, speed_ms, heading, cf_ts, local_iso)
        (1, "46.193000", "6.129000", "12", "0.0", "0", "763689600", "2026-03-13 20:00:00+01"),
        (2, "46.193400", "6.128800", "25", "0.4", "310", "763700400", "2026-03-13 23:00:00+01"),
        (3, "46.198800", "6.135200", "35", "9.1", "40", "763711080", "2026-03-14 01:58:00+01"),
        (4, "46.202400", "6.140300", "18", "1.1", "15", "763712700", "2026-03-14 02:25:00+01"),
        (5, "46.202800", "6.140100", "22", "0.2", "0", "763713600", "2026-03-14 02:40:00+01"),
        (6, "46.197500", "6.133000", "30", "11.3", "220", "763714800", "2026-03-14 03:00:00+01"),
    ]
    for path, inner, arch in (
            ("/case/exhibit_A/cache.sqlite", None, None),
            ("/case/exhibit_A_copy.zip", "exhibit_A_copy.zip!cache.sqlite", True)):
        sid = b.source(path, inner, None, "sqlite", "ZRTCLLOCATIONMO", "fp-cache-9f31",
                       "EX-A/1")
        for line, lat, lon, acc, sp, hd, cf, iso in fixes:
            rr = b.record(sid, line, {"ZLATITUDE": lat, "ZLONGITUDE": lon,
                "ZHORIZONTALACCURACY": acc, "ZSPEED": sp, "ZCOURSE": hd, "ZTIMESTAMP": cf})
            ent = b.entity(rr, 0, None, None, "none", "device")
            sx = b.spatial(rr, 0, "coordinate", "epsg:4326", "GNSS",
                [("latitude", None, "ZLATITUDE", lat),
                 ("longitude", None, "ZLONGITUDE", lon),
                 ("horizontal_accuracy_m", None, "ZHORIZONTALACCURACY", acc),
                 ("horizontal_speed_kmh", "m/s", "ZSPEED", sp),
                 ("heading_deg", None, "ZCOURSE", hd)],
                "passthrough_wgs84/1.0+unit_convert/1.0", float(lat), float(lon),
                horizontal_accuracy_m=float(acc),
                horizontal_speed_kmh=round(float(sp) * 3.6, 2), heading_deg=float(hd))
            tx = b.temporal(rr, 0, "instant", "apple_cfabsolute", "internal_clock",
                [("lower", None, "ZTIMESTAMP", cf)],
                "epoch_cfabsolute/1.0", us_from_iso(iso), us_from_iso(iso))
            a = b.observation(rr, p_cache, "ios_cache", 0, ent, sx, tx,
                "subject", "measured", "at_least_once", label="iOS cache")
            b.analysis(a, "untangle/2.0", 1)

    # ---- Visit with entry & exit (Local.sqlite) --------------------------
    sid = b.source("/case/exhibit_A/local.sqlite", None, None, "sqlite",
                   "ZRTVISITMO", "fp-local-7b02", "EX-A/1")
    rr = b.record(sid, 1, {"ZLAT": "46.193000", "ZLON": "6.129000",
                           "ZENTRYDATE": "763689600", "ZEXITDATE": "763702200"})
    ent = b.entity(rr, 0, None, None, "none", "device")
    sx = b.spatial(rr, 0, "coordinate", "epsg:4326", "GNSS",
        [("latitude", None, "ZLAT", "46.193000"),
         ("longitude", None, "ZLON", "6.129000")],
        "passthrough_wgs84/1.0+unit_convert/1.0", HOME[0], HOME[1],
        horizontal_accuracy_m=40.0)
    # Each bound carries its OWN role: lower=check_in, upper=check_out. An epoch is
    # absolute UTC — the source states no zone, so stated_offset is None ('(none)').
    tx = b.temporal(rr, 0, "interval", "apple_cfabsolute", "internal_clock",
        [("lower", None, "ZENTRYDATE", "763689600"),
         ("upper", None, "ZEXITDATE", "763702200")],
        "epoch_cfabsolute/1.0", us_from_iso("2026-03-13 20:00:00+01"),
        us_from_iso("2026-03-13 23:30:00+01"))
    b.observation(rr, p_visit, "ios_visits", 0, ent, sx, tx,
        "subject", "measured", "throughout", label="iOS visits")

    # ---- CDRs: operator A file (3 records; #2 is THE call near the scene) -
    def cdr(sid, line, a_msisdn, b_msisdn, imei, start_iso, dur, cell, coords,
            preset_id, preset_key, azimuth=120, beam_width=65):
        rr = b.record(sid, line, {"a_party": a_msisdn, "b_party": b_msisdn,
            "imei": imei, "start": start_iso[:-3], "duration": str(dur), "cell": cell})
        # The callee and the caller's own IMEI have NO home since decision 51 removed
        # activity: they are non-positional parties, and that gap is deliberately open.
        ent = b.entity(rr, 0, a_msisdn.replace(" ", ""), a_msisdn, "msisdn", "device")
        # The BEARER: the serving antenna. It holds the position the record states, so it
        # gets a real entity row instead of surviving only as a cell_ref string — which is
        # what makes "which other handsets hit this cell?" a GROUP BY rather than a
        # string match against a raw component value.
        bearer = b.entity(rr, 1, cell, cell, "cell", "antenna")
        sx = b.spatial(rr, 0, "cell_id", None, "cell",
            [("raw_position", None, "cell", cell)],
            "unit_convert/1.0", None, None,
            beam_azimuth_deg=float(azimuth), beam_width_deg=float(beam_width))
        lo = us_from_iso(start_iso)
        tx = b.temporal(rr, 0, "interval", "local_string", "NTP",
            [("lower", "+01:00", "start", start_iso[:-3]),
             ("duration", "+01:00", "duration", str(dur))],
            "tz_localize/tzdb2026a+start_plus_duration/1.0",
            lo, lo + dur * 1_000_000)
        a = b.observation(rr, preset_id, preset_key, 0, ent, sx, tx,
            "party", "served", "at_least_once", bearer_pair=bearer,
            label="CDR op-A")
        b.analysis(a, "untangle/2.0", 1)
        return a

    sid = b.source("/case/operatorA_cdr.csv", None, None, "csv", None,
                   "fp-cdrA-11aa", "REQ-77")
    cdr(sid, 1, "+41 79 123 45 67", "+33 6 12 34 56 78", "352099001761481",
        "2026-03-13 18:40:12+01", 312, "228-01-2042-70110", (46.2280, 6.1520),
        p_cdr, "cdr_generic")
    cdr(sid, 2, "+41 79 123 45 67", "+41 76 222 33 44", "352099001761481",
        "2026-03-14 02:20:31+01", 187, "228-01-2042-77341", CELL_A, p_cdr, "cdr_generic")
    cdr(sid, 3, "+41 79 123 45 67", "+33 6 12 34 56 78", "352099001761481",
        "2026-03-14 11:05:00+01", 95, "228-01-2042-70110", (46.2280, 6.1520),
        p_cdr, "cdr_generic")

    # ---- operator B disclosure of THE SAME 02:20 call (B-leg cell) --------
    sid = b.source("/case/operatorB_cdr.csv", None, None, "csv", None,
                   "fp-cdrB-22bb", "REQ-78")
    rr = b.record(sid, 1, {"caller": "+41791234567", "callee": "+41762223344",
        "start": "2026-03-14 02:20:31", "duration": "187", "cell_b": "228-02-1011-55221"})
    ent = b.entity(rr, 0, "+41762223344", "+41762223344", "msisdn", "device")
    # The B-leg's own serving antenna — a different cell from the A-leg's.
    bearer = b.entity(rr, 1, "228-02-1011-55221", "228-02-1011-55221", "cell", "antenna")
    sx = b.spatial(rr, 0, "cell_id", None, "cell",
        [("raw_position", None, "cell_b", "228-02-1011-55221")],
        "unit_convert/1.0", None, None,
        beam_azimuth_deg=300.0, beam_width_deg=65.0)
    lo = us_from_iso("2026-03-14 02:20:31+01")
    tx = b.temporal(rr, 0, "interval", "local_string", "NTP",
        [("lower", "+01:00", "start", "2026-03-14 02:20:31"),
         ("duration", "+01:00", "duration", "187")],
        "tz_localize/tzdb2026a+start_plus_duration/1.0",
        lo, lo + 187_000_000)
    b.observation(rr, p_cdr, "cdr_generic", 0, ent, sx, tx,
        "party", "served", "at_least_once", bearer_pair=bearer,
        label="CDR op-B")

    # ---- overlapping disclosure: operator A supplement re-contains #2 -----
    # WHY: same claim in a different file -> equal spatiotemporal_entity_hash, distinct observation_content_hash
    # (different fingerprint) — a possible-same-evidence finding, never a merge.
    sid = b.source("/case/operatorA_cdr_supplement.csv", None, None, "csv", None,
                   "fp-cdrA2-33cc", "REQ-77bis")
    cdr(sid, 1, "+41 79 123 45 67", "+41 76 222 33 44", "352099001761481",
        "2026-03-14 02:20:31+01", 187, "228-01-2042-77341", CELL_A, p_cdr, "cdr_generic")

    # ---- ANPR: three reads of the suspect's plate ------------------------
    # The camera IS the spatial expression: its id is the cell_ref/asset_ref
    # component — no detector mention row exists any more.
    sid = b.source("/case/anpr_export.csv", None, None, "csv", None,
                   "fp-anpr-44dd", "REQ-90")
    for line, cam, ts, coords in (
            (1, "CAM-A7-KM12", "2026-03-14 02:05:33+01", CAM_A7),
            (2, "CAM-A7-KM12", "2026-03-14 02:52:10+01", CAM_A7),
            (3, "CAM-B2-KM03", "2026-03-12 17:22:05+01", (46.1850, 6.1210))):
        rr = b.record(sid, line, {"camera_id": cam, "plate": "GE 123 456",
            "ts": ts[:-3], "ocr_conf": "0.92"})
        ent = b.entity(rr, 0, "GE123456", "GE 123 456", "plate", "vehicle")
        # The BEARER: the camera. The plate was seen BY it; the position is the camera's.
        bearer = b.entity(rr, 1, cam, cam, "camera", "camera")
        sx = b.spatial(rr, 0, "unknown", None, "camera",
            [("raw_position", None, "camera_id", cam)],
            _UNAVAILABLE, None, None)
        tx = b.temporal(rr, 0, "instant", "local_string", "NTP",
            [("lower", "+01:00", "ts", ts[:-3])],
            "tz_stated_offset/1.0", us_from_iso(ts), us_from_iso(ts))
        a = b.observation(rr, p_anpr, "anpr_ch", 0, ent, sx, tx,
            "party", "seen", "at_least_once", bearer_pair=bearer, label="ANPR")
        b.analysis(a, "untangle/2.0", 1)

    # ---- CCTV at the scene -----------------------------------------------
    sid = b.source("/case/dvr_index.csv", None, None, "csv", None,
                   "fp-dvr-55ee", "REQ-91")
    rr = b.record(sid, 1, {"channel": "Lobby-2", "site": "Rue du Stand 15, 1204 Geneve",
        "start": "2026-03-14 02:25:00", "end": "2026-03-14 02:50:00"})
    ent = b.entity(rr, 0, "Lobby-2", "Lobby-2", "camera", "camera")
    sx = b.spatial(rr, 0, "street_address", None, "manual",
        [("raw_position", None, "site", "Rue du Stand 15, 1204 Geneve")],
        _UNAVAILABLE, None, None)
    tx = b.temporal(rr, 0, "interval", "local_string", "internal_clock",
        [("lower", None, "start", "2026-03-14 02:25:00"),
         ("upper", None, "end", "2026-03-14 02:50:00")],
        "tz_localize/tzdb2026a", us_from_iso("2026-03-14 02:25:00+01"),
        us_from_iso("2026-03-14 02:50:00+01"))
    b.observation(rr, p_dvr, "dvr_generic", 0, ent, sx, tx,
        "subject", "located", "throughout", label="CCTV")

    # ---- robbery in the police register ----------------------------------
    # the event IS the entity (identifier == concerns, identifier_type=case_ref);
    # the victim and the offence stay verbatim in raw_json -> orig_. No activity row:
    # a single placement has nothing to group (activity is a grouping bonus).
    sid = b.source("/case/police_register.csv", None, None, "csv", None,
                   "fp-pol-66ff", "REQ-92", tier="secondary")
    rr = b.record(sid, 1, {"case": "GE-2026-04412", "offense": "robbery",
        "location": "Rue du Stand 15, Geneve", "from": "2026-03-14 02:30",
        "to": "2026-03-14 02:45", "victim": "M. Keller"})
    ent = b.entity(rr, 0, "GE-2026-04412", "GE-2026-04412", "case_ref", "event",
                   concerns="GE-2026-04412", concerns_origin="preset_mapped")
    sx = b.spatial(rr, 0, "street_address", None, "manual",
        [("raw_position", None, "location", "Rue du Stand 15, Geneve")],
        _UNAVAILABLE, None, None)
    tx = b.temporal(rr, 0, "interval", "local_string", "manual",
        [("lower", None, "from", "2026-03-14 02:30"),
         ("upper", None, "to", "2026-03-14 02:45")],
        "tz_localize/tzdb2026a", us_from_iso("2026-03-14 02:30:00+01"),
        us_from_iso("2026-03-14 02:45:00+01"))
    b.observation(rr, p_police, "police_reg", 0, ent, sx, tx,
        "subject", "located", "throughout",
        label="police register")

    # ---- witness audition ------------------------------------------------
    sid = b.source("/case/audition_pv.csv", None, None, "csv", None,
                   "fp-wit-7700", "REQ-93", tier="secondary")
    rr = b.record(sid, 1, {"witness": "Marie Dupont",
        "statement": "J'ai vu l'homme pres de la gare vers midi le 14 mars"})
    # concerns = the reporter (Marie Dupont), fixed at parsing.
    ent = b.entity(rr, 0, None, "l'homme", "none", "person",
                   concerns="Marie Dupont", concerns_origin="preset_mapped")
    sx = b.spatial(rr, 0, "unknown", None, "manual",
        [("raw_position", None, "statement", "pres de la gare")],
        _UNAVAILABLE, None, None)
    tx = b.temporal(rr, 0, "interval", "local_string", "manual",
        [("lower", "+01:00", "statement", "vers midi le 14 mars"),
         ("upper", "+01:00", "statement", "vers midi le 14 mars")],
        "fuzzy_time/1.0", us_from_iso("2026-03-14 11:00:00+01"),
        us_from_iso("2026-03-14 13:00:00+01"),
        # the fuzzy_time reader assigns ±1 h to EACH bound of "vers midi" — a METHOD-level
        # accuracy claim (no L1 component), so it lands per bound only at L2.
        lower_accuracy_us=3600_000_000, upper_accuracy_us=3600_000_000)
    b.observation(rr, p_audit, "audition", 0, ent, sx, tx,
        # "vers midi": the statement places the man there at SOME point around noon, not for
        # the whole fuzzy window.
        "subject", "located", "at_least_once",
        label="audition")

    # ---- shared pin in instant messaging (REFERENCED, never presence) ----
    sid = b.source("/case/exhibit_A/chat.db", None, None, "sqlite", "messages",
                   "fp-chat-8811", "EX-A/1")
    rr = b.record(sid, 1, {"sender": "+41791234567", "recipient": "+41762223344",
        "ts": "1773450680", "lat": "46.203000", "lon": "6.140000",
        "caption": "meet me here", "is_deleted": "1"})
    ent = b.entity(rr, 0, "+41791234567", "+41791234567", "msisdn", "device")
    sx = b.spatial(rr, 0, "coordinate", "epsg:4326", "GNSS",
        [("latitude", None, "lat", "46.203000"),
         ("longitude", None, "lon", "6.140000")],
        "passthrough_wgs84/1.0", SCENE[0], SCENE[1])
    tx = b.temporal(rr, 0, "instant", "unix_epoch_s", "NTP",
        [("lower", None, "ts", "1773450680")],   # epoch: no stated zone
        "epoch_unix_s/1.0", 1773450680_000_000, 1773450680_000_000)
    # The chat app reports this row as recovered rather than read in place — a SECONDARY
    # source, so the value is the TOOL's report, stated by the preset (decision 48). The old
    # `deleted` column asked a question the export could not answer separately from this one.
    pin = b.entity(rr, 1, "scene-pin", "scene-pin", "none", "poi")
    b.observation(rr, p_im, "im_generic", 0, ent, sx, tx,
        "party", "shared", "at_least_once",
        bearer_pair=pin, recovery_state="recovered", recovery_source="freelist",
        label="chat pin")

    # ---- route navigation search — TWO places (origin, destination) ------
    sid = b.source("/case/exhibit_A/maps_history.db", None, None, "sqlite",
                   "searches", "fp-maps-9922", "EX-A/1")
    rr = b.record(sid, 1, {"account": "jdoe@gmail.com",
        "query_ts": "2026-03-13T21:04:09Z", "origin": "Home",
        "destination": "Rue du Stand 15, Geneve"})
    for ordinal, (kind, field, rawv, geo) in enumerate((
            ("street_address", "destination", "Rue du Stand 15, Geneve", SCENE),
            # A bare label, neither an address nor a coordinate.
            ("unknown", "origin", "Home", None))):
        ent = b.entity(rr, ordinal, "jdoe@gmail.com", "jdoe@gmail.com", "email", "account")
        sx = b.spatial(rr, ordinal, kind, None, "manual",
            [("raw_position", None, field, rawv)],
            _UNAVAILABLE, None, None)
        tx = b.temporal(rr, ordinal, "instant", "iso8601", "NTP",
            [("lower", "+00:00", "query_ts", "2026-03-13T21:04:09Z")],
            "passthrough_utc/1.0", us_from_iso("2026-03-13 21:04:09+00"),
            us_from_iso("2026-03-13 21:04:09+00"))
        target = b.entity(rr, ordinal + 10, rawv, rawv, "none", "poi")
        b.observation(rr, p_nav, "nav_generic", ordinal, ent, sx, tx,
            "party", "searched", "at_least_once",
            bearer_pair=target, label="nav search")

    # ---- an explicit alibi: temporal_link = never (the only negation) ---
    # WHY: gives presence queries something to EXCLUDE — the suspect's phone was NOT at
    # home during the robbery window (a negated claim; the graph layer is deferred).
    sid = b.source("/case/alibi_statement.csv", None, None, "csv", None,
                   "fp-alibi-bb44", "REQ-94", tier="secondary")
    rr = b.record(sid, 1, {"subject": "+41791234567", "place": "Home",
        "from": "2026-03-14 02:30", "to": "2026-03-14 02:45",
        "claim": "not present"})
    ent = b.entity(rr, 0, "+41791234567", "+41791234567", "msisdn", "device")
    sx = b.spatial(rr, 0, "unknown", None, "manual",
        [("raw_position", None, "place", "Home")],
        _UNAVAILABLE, None, None)
    tx = b.temporal(rr, 0, "interval", "local_string", "manual",
        [("lower", "+01:00", "from", "2026-03-14 02:30"),
         ("upper", "+01:00", "to", "2026-03-14 02:45")],
        "tz_localize/tzdb2026a", us_from_iso("2026-03-14 02:30:00+01"),
        us_from_iso("2026-03-14 02:45:00+01"))
    # Home holds the position; the device is a party to it, and the ONLY negation is the
    # temporal link (decision 55): never located there during the window.
    home = b.entity(rr, 1, "Home", "Home", "none", "poi")
    b.observation(rr, p_audit, "audition", 0, ent, sx, tx,
        "party", "located", "never", bearer_pair=home,
        label="alibi")

    # ---- one edit_log row: an analyst corrected a geocode in place --
    # Integration rows MAY update in place; the change is audited, evidence untouched.
    # the ordinal makes the journal totally ordered, so replaying inverses in
    # reverse ordinal order rewinds the DB to any earlier state.
    cx.execute("INSERT INTO edit_log VALUES (?,?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), 1, "spatial_integration", "(demo)", "latitude_wgs84",
         "46.2029", "46.2030", "update", "insp. Girard", "2026-03-15T09:12:00Z"))

    # The self-description: DERIVED from the model, so the demo DB explains itself exactly
    # as a real run DB does and cannot drift from the schema it illustrates.
    from model_atlas.core.schema import model_description_rows
    cx.execute("CREATE TABLE model_description ("
               "table_name TEXT NOT NULL, field_name TEXT, type TEXT, unit TEXT, "
               "matlas_enrichment INTEGER, description TEXT NOT NULL, "
               "UNIQUE (table_name, field_name));")
    cx.executemany("INSERT INTO model_description "
                   "(table_name, field_name, type, unit, matlas_enrichment, description) "
                   "VALUES (?,?,?,?,?,?);", model_description_rows())

    cx.commit()
    return cx


# --------------------------------------------------------------------------- #
# Demonstration queries — the Q1..Q10 story, new projection columns           #
# --------------------------------------------------------------------------- #

def local_clock(bound: str, alias: str = "ti") -> str:
    """SQL that renders ONE bound as its local wall clock, in the query.

    This is THE point of the two-column design: the case stores the canonical UTC µs and
    the offset to local, and whoever displays a time adds them — so a report always shows
    the currently configured zone rather than one frozen into the row at ingest. The `N/A`
    branch keeps "no local zone is configured" visible instead of silently showing UTC as
    if it were local.
    """
    us, off = f"{alias}.{bound}_unix_utc_us", f"{alias}.offset_to_local_hours_{bound}"
    return (f"CASE WHEN {us} IS NULL THEN ''"
            f" WHEN {off} = 'N/A'"
            f" THEN datetime({us}/1000000, 'unixepoch') || ' N/A'"
            f" ELSE datetime({us}/1000000 + CAST({off} AS REAL)*3600, 'unixepoch')"
            f" || printf('%+03d:00', CAST({off} AS INTEGER)) END")


# The flat projection (the flat CSV column contract): one line per observation. The BEARER
# columns come from the SECOND entity join — storage orders by whose position it is, the
# projection by who is under investigation, so a CDR line still names the phone while the
# antenna arrives as a column. Every column mATLAS ADDED rather than read from the trace
# carries the `_matlas` suffix; a value derived from a trace datum in an obvious way
# (`entity`, `lat`/`lon`) keeps its bare name. The times are RENDERED by the query from
# the canonical UTC µs + the offset to local — there is no stored local-clock column.
FLAT = f"""
SELECT substr(a.observation_id,1,8) AS row_uid_matlas,
  e.identifier AS entity, e.raw_identifier, e.identifier_type, e.type AS entity_type,
  a.entity_link AS entity_link_matlas, a.spatial_link AS spatial_link_matlas,
  a.temporal_link AS temporal_link_matlas,
  be.identifier AS bearer_entity, be.type AS bearer_entity_type,
  si.latitude_wgs84 AS lat, si.longitude_wgs84 AS lon,
  six.s2_cell_id AS s2_cell_id_matlas,
  {local_clock("lower")} AS t_lower, {local_clock("upper")} AS t_upper,
  a.source_label AS source_label_matlas
FROM observation a
JOIN entity e ON e.entity_id=a.entity_id
LEFT JOIN entity be ON be.entity_id=a.bearer_entity_id
LEFT JOIN spatial_integration si ON si.spatial_expression_id=a.spatial_expression_id
LEFT JOIN spatial_index six ON six.spatial_integration_id=si.spatial_integration_id
LEFT JOIN timespan_integration ti ON ti.temporal_expression_id=a.temporal_expression_id
"""

_LO = us_from_iso("2026-03-14 02:00:00+01")
_HI = us_from_iso("2026-03-14 03:10:00+01")

QUERIES = [
 ("Q1 - The flat projection (one line per placement); the whole example run",
  FLAT + "ORDER BY ti.lower_unix_utc_us, a.source_label, row_uid_matlas"),

 ("Q2 - Timeline of ONE identifier across every source (convergence by GROUP-able "
  "normalized identifier, no stored merge)",
  FLAT + "WHERE e.identifier_type='msisdn' AND e.identifier='+41791234567' "
         "ORDER BY ti.lower_unix_utc_us, a.source_label, row_uid_matlas"),

 ("Q3 - WHO WAS AT THE SCENE 02:00-03:10? Positive claims with a contact verb only: "
  "searched/shared/cached places AND the negated alibi excluded. (An explicit filter, "
  "not the engine's presence predicate, which decision 55 left to design.)",
  FLAT + f"""WHERE si.latitude_wgs84 BETWEEN 46.198 AND 46.208
  AND si.longitude_wgs84 BETWEEN 6.133 AND 6.146
  AND ti.lower_unix_utc_us <= {_HI} AND ti.upper_unix_utc_us >= {_LO}
  AND a.temporal_link IN ('throughout','at_least_once')
  AND a.spatial_link IN ('measured','registered','served','seen','used','stayed','located')
ORDER BY ti.lower_unix_utc_us, a.source_label, row_uid_matlas"""),

 ("Q4 - ...and the places only searched, shared or cached at the same spot (leads, "
  "not presence)",
  FLAT + """WHERE si.latitude_wgs84 BETWEEN 46.198 AND 46.208
  AND si.longitude_wgs84 BETWEEN 6.133 AND 6.146
  AND a.spatial_link IN ('searched','shared','cached')
ORDER BY row_uid_matlas"""),

 ("Q5 - Duplicate evidence findings (never merged, always reported; duplicates SURFACE)",
  "SELECT 'same file bytes' AS finding, fingerprint_sha256 AS digest, copies, files "
  "FROM duplicate_sources "
  "UNION ALL "
  "SELECT 'possible same evidence (file-independent)', substr(spatiotemporal_entity_hash,1,16), "
  "copies, files FROM possible_same_evidence ORDER BY finding, digest"),

 ("Q6 - Convergence by normalized identifier: every mention of +41791234567, with its "
  "VERBATIM raw_identifier surfaced (same working id, different source text)",
  """SELECT e.identifier, e.raw_identifier, e.identifier_type, e.type,
     a.source_label
FROM observation a JOIN entity e ON e.entity_id=a.entity_id
WHERE e.identifier_type='msisdn' AND e.identifier='+41791234567'
ORDER BY a.source_label, e.raw_identifier"""),

 ("Q7 - Which identifiers used tower 228-01-2042-77341? (infrastructure archaeology; "
  "duplicates surface, no DISTINCT)",
  f"""SELECT e.identifier AS entity,
     {local_clock("lower")} AS at_local, a.source_label, s.input_file_path
FROM spatial_component sc
JOIN spatial_expression sx ON sx.spatial_expression_id=sc.spatial_expression_id
JOIN observation a ON a.spatial_expression_id=sx.spatial_expression_id
JOIN entity e ON e.entity_id=a.entity_id
JOIN timespan_integration ti ON ti.temporal_expression_id=a.temporal_expression_id
JOIN raw_record r ON r.raw_record_id=a.raw_record_id
JOIN source_file s ON s.source_file_id=r.source_file_id
WHERE sc.raw_value='228-01-2042-77341'
ORDER BY ti.lower_unix_utc_us, s.input_file_path"""),

 ("Q8 - Trace one CSV value back to the source (the 02:20 tower position), through the "
  "expression/component/integration/S2 chain",
  """SELECT si.method, si.latitude_wgs84, si.longitude_wgs84, six.s2_cell_id, six.s2_level,
     sc.part, sc.unit, sc.source_field, sc.raw_value AS verbatim,
     r.line_no, s.input_file_path
FROM spatial_integration si
JOIN spatial_expression sx ON sx.spatial_expression_id=si.spatial_expression_id
JOIN spatial_component sc ON sc.spatial_expression_id=sx.spatial_expression_id
JOIN spatial_index six ON six.spatial_integration_id=si.spatial_integration_id
JOIN observation a ON a.spatial_expression_id=sx.spatial_expression_id
JOIN raw_record r ON r.raw_record_id=a.raw_record_id
JOIN source_file s ON s.source_file_id=r.source_file_id
WHERE sc.raw_value='228-01-2042-77341'
  AND s.input_file_path LIKE '%operatorA_cdr.csv'"""),

 ("Q9 - The two temporal facts side by side: the offset the SOURCE stated (L1, verbatim) "
  "vs the offset to the CASE's local zone (L2) and the clock it renders",
  f"""SELECT a.source_label, tc.part, tc.stated_offset,
     ti.lower_unix_utc_us,
     ti.offset_to_local_hours_lower, ti.offset_to_local_hours_upper,
     {local_clock("lower")} AS lower_local, {local_clock("upper")} AS upper_local
FROM observation a
JOIN temporal_expression tx ON tx.temporal_expression_id=a.temporal_expression_id
JOIN temporal_component tc ON tc.temporal_expression_id=tx.temporal_expression_id
JOIN timespan_integration ti ON ti.temporal_expression_id=tx.temporal_expression_id
WHERE a.source_label IN ('iOS cache','CDR op-A','chat pin')
ORDER BY a.source_label, tc.ordinal
LIMIT 12"""),

 ("Q10 - The analysis layer: the current ranker's verdict per observation (re-running a "
  "changed ranker ADDS rows, never mutates; the CSV projects the current method)",
  """SELECT an.method, an.record_rank,
     CASE WHEN an.record_rank = 1 THEN 'main' ELSE 'additional' END AS record_order,
     e.type AS entity_type,
     a.source_label, COUNT(*) AS n
FROM analysis an
JOIN observation a ON a.observation_id=an.observation_id
JOIN entity e ON e.entity_id=a.entity_id
GROUP BY an.method, an.record_rank, e.type, a.source_label
ORDER BY an.method, a.source_label"""),
]


def print_queries(cx: sqlite3.Connection) -> None:
    for title, sql in QUERIES:
        cur = cx.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        print(f"\n### {title}\n")
        print("| " + " | ".join(cols) + " |")
        print("|" + "---|" * len(cols))
        for row in rows:
            print("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
        print(f"\n({len(rows)} rows)")


def write_flat_csv() -> None:
    """Export the flat CSV beside the database, through the REAL exporter brick.

    WHY the real brick and not a hand-rolled dump: the committed CSV is a reference for
    what a run actually produces, so it has to come from the same projection and the same
    writer an operator gets — a second, parallel dump would drift from the contract the
    moment either changed, and would then teach the wrong shape.

    WHY it is regenerated here rather than checked in independently: the database and its
    projection must never disagree. Building one always rebuilds the other.

    WHY it goes through ``build_flat_view`` + the exporter's own ``_write`` rather than
    ``CsvFlatExporter.export``: that entry point takes a ``RunStore``, and
    ``RunStore.open_persisted`` authenticates the file by its ``application_id`` /
    ``user_version`` stamp — which this hand-built fixture deliberately does not carry, so
    it is never mistaken for a real run database. The two calls below ARE the body of
    ``export``: the same projection and the same writer, so the bytes are identical to a
    real run's, without pretending the fixture is one.
    """
    import sqlite3

    from model_atlas.core.flat_view import build_flat_view
    from model_atlas.export.csv_flat import CsvFlatExporter

    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        frame = build_flat_view(connection, include_source_columns=True, ranking_method=None)
    finally:
        connection.close()
    written = CsvFlatExporter()._write(frame, CSV)
    print(f"exported {written} ({len(frame)} rows, {len(frame.columns)} columns)",
          file=sys.stderr)


if __name__ == "__main__":
    cx = build()
    # A diagnostic to stderr; the queries table on stdout is the deliverable (a result,
    # not a log — so print() is correct there per the logging-over-print exception).
    print(f"built {DB}", file=sys.stderr)
    if "--queries" in sys.argv:
        print_queries(cx)
    cx.close()
    write_flat_csv()
