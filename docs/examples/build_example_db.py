#!/usr/bin/env python3
"""Deterministic worked-example run database for the settled 21-table model.

WHAT THIS FILE IS
-----------------
A hand-written **demonstration artefact** — *not Phase-1 code*. It builds
``docs/examples/matlas_example.sqlite`` directly from the settled schema
([../run_db_schema.md](../run_db_schema.md), decisions 16-41) so the model can be
exercised with realistic data *before* ``model/`` derives the real DDL
from ``families.py``. The DDL, the digest recipes and the ingest order are transcribed
by hand here; when the core lands, this script is superseded by the pipeline and kept
only as a readable reference.

USED BY
-------
- ``docs/examples/example_queries.md`` — regenerated from ``--queries`` output.
- ``docs/model_worked_examples.md`` — every row-by-layer walk is spot-checked against
  the rows this builder writes.

DEPENDS ON
----------
- the settled schema doc (transcribed below) and ``s2sphere`` (D31 — the S2 leaf cell
  per spatial_integration; pure-Python, added to the project deps for T31).

DETERMINISM (the whole point)
-----------------------------
Every id is ``uuid5`` over natural-key parts joined with the unit separator ``\\x1f``
(D-"id scheme"; a ``|`` in a path could otherwise forge equal keys). The *only*
non-deterministic roots are ``case`` and ``run`` (uuid4 — the run scope, D10/D28);
no EVIDENCE id derives from them, so running the builder twice yields byte-identical
DBs apart from those two rows and the run-scoped metadata that carries the run id
(``run_archive``/``run_preset`` links, ``source.run_id``, ``edit_log``). Floats are captured as **source text** (never re-parsed) so the
canonical-JSON rule (schema §Determinism rule 5, no float re-formatting) holds and the
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

import s2sphere

DB = Path(__file__).parent / "matlas_example.sqlite"

# One namespace per id space keeps two logically distinct rows that happen to share
# natural-key text from ever colliding (schema §"The id scheme": one namespace per table).
NS = uuid.UUID("9c4e7b1a-2d63-5f48-8a17-3b5c6d0e9f21")
SEP = "\x1f"  # unit separator — a delimiter that cannot occur inside a path or a value

# S2 leaf level (D31): CellId.from_lat_lng always returns a level-30 leaf cell.
S2_LEAF_LEVEL = 30


def u5(table: str, *parts: object) -> str:
    """uuid5 over (table-tag ⟂ natural-key parts). Deterministic across runs."""
    joined = SEP.join(table if i == 0 else _txt(p) for i, p in enumerate((table, *parts)))
    return str(uuid.uuid5(NS, joined))


def _txt(part: object) -> str:
    # None becomes the empty string so a missing part is distinct-but-stable, never a
    # literal "None" that could clash with a real value.
    return "" if part is None else str(part)


def s2_signed_leaf(lat: float, lon: float) -> int:
    """Signed-int64 S2 leaf cell id for (lat, lon).

    HOW: s2sphere returns an *unsigned* 64-bit cell id; SQLite INTEGER is signed, so we
    reinterpret the top-bit-set values as negative (v - 2**64 when v >= 2**63). WHY:
    storing the same 64 bits SQLite will compare and range-scan on (D31 relies on
    ``WHERE s2_cell_id BETWEEN ? AND ?``); a naive unsigned store would overflow.
    """
    cell = s2sphere.CellId.from_lat_lng(s2sphere.LatLng.from_degrees(lat, lon))
    v = cell.id()
    return v - 2**64 if v >= 2**63 else v


# --------------------------------------------------------------------------- #
# Canonical JSON + the normative L1 projection (the digest recipes)           #
# --------------------------------------------------------------------------- #

def canonical_json(obj: object) -> str:
    """Canonical JSON per schema §Determinism rule 5: sorted keys, no insignificant
    whitespace. Float source-text is preserved by keeping numbers as strings upstream,
    so json.dumps never re-formats a value it was given."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def l1_projection(*, entity, spatial, temporal, links, deleted, multiplicity,
                  activity) -> str:
    """The NORMATIVE L1 projection both digests hash (schema §"The normative L1
    projection"), in the exact field order — and only those fields.

    Excluded normatively: concerns / concerns_uid / both origins, raw_identifier,
    component source_fields, all paths, all ids/run_id, all integrations, all analysis.
    Those are deliberately absent below so a register/tz upgrade, an operator param, or
    a renamed source column can never move an evidence digest.
    """
    parts: list[str] = []

    # entity corner: identifier ⟂ identifier_type ⟂ type ⟂ entity_role
    parts += [_txt(entity["identifier"]), _txt(entity["identifier_type"]),
              _txt(entity["type"]), _txt(links["entity_role"])]

    # spatial expression: referential ⟂ spatial_source ⟂ components(part, role, raw)
    if spatial is None:
        parts += ["", ""]
    else:
        parts += [_txt(spatial["referential"]), _txt(spatial["spatial_source"])]
        for c in spatial["components"]:  # already ordinal-ordered
            parts += [_txt(c["part"]), _txt(c["spatial_role"]), _txt(c["raw_value"])]

    # temporal expression: kind ⟂ referential ⟂ components(part, role, stated_offset, raw)
    if temporal is None:
        parts += ["", ""]
    else:
        parts += [_txt(temporal["kind"]), _txt(temporal["referential"])]
        for c in temporal["components"]:
            parts += [_txt(c["part"]), _txt(c["temporal_role"]),
                      _txt(c["stated_offset"]), _txt(c["raw_value"])]

    # the three links ⟂ deleted ⟂ multiplicity
    parts += [_txt(links["entity_position_link"]), _txt(links["entity_time_link"]),
              _txt(links["spatial_temporal_link"]), _txt(deleted), _txt(multiplicity)]

    # activity: kind ⟂ canonical context_json
    if activity is None:
        parts += ["", ""]
    else:
        ctx = activity.get("context_json")
        parts += [_txt(activity["kind"]),
                  "" if ctx is None else canonical_json(ctx)]

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


def local_projection(unix_us: int | None, offset_hours: float | str | None):
    """D32 convenience projection for ONE bound: (datetime_iso, date, time_us).

    HOW: from canonical UTC µs + the applied offset we render the local wall clock.
    - no instant (unix_us is None)        -> ("", None, None)   (empty everywhere)
    - offset known (a float)              -> "…+01:00" ISO + local date + time-of-day µs
    - offset UNKNOWN (the "N/A" sentinel) -> assumed-UTC clock with a LOUD trailing " N/A"
    WHY: the ` N/A` suffix keeps "value, zone unknown" visibly distinct from "no value"
    (empty) — the two must never be confused (D32).
    """
    if unix_us is None:
        return "", None, None
    known = isinstance(offset_hours, (int, float))
    off = float(offset_hours) if known else 0.0
    local_us = unix_us + int(round(off * 3600 * 1_000_000))
    dt = datetime.fromtimestamp(local_us / 1_000_000, tz=timezone.utc)  # already shifted
    if known:
        sign = "+" if off >= 0 else "-"
        oh, om = divmod(int(round(abs(off) * 60)), 60)
        iso = dt.strftime("%Y-%m-%dT%H:%M:%S") + f"{sign}{oh:02d}:{om:02d}"
    else:
        # zone unknown: show the assumed-UTC clock, flagged loudly.
        iso = dt.strftime("%Y-%m-%dT%H:%M:%S") + " N/A"
    date_us = int(datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc).timestamp()
                  * 1_000_000)
    time_us = local_us - date_us
    return iso, date_us, time_us


# --------------------------------------------------------------------------- #
# DDL — the 19 tables + matlas_meta, transcribed from run_db_schema.md        #
# --------------------------------------------------------------------------- #

DDL = """
PRAGMA foreign_keys = ON;

-- Layer 0 ------------------------------------------------------------------
CREATE TABLE case_metadata (case_id TEXT PRIMARY KEY, case_number TEXT NOT NULL,
    title TEXT, description TEXT, created_at TEXT);
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
CREATE TABLE source (source_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES run(run_id),
    input_file_path TEXT NOT NULL, inner_path TEXT, data_source TEXT,
    format TEXT, subunit TEXT, fingerprint_sha256 TEXT,
    sibling_fingerprints_json TEXT, exhibit_ref TEXT, source_tier TEXT,
    record_count INTEGER,
    UNIQUE (run_id, input_file_path, inner_path, subunit));
CREATE INDEX ix_source_fp ON source(fingerprint_sha256);
CREATE TABLE preset (preset_id TEXT PRIMARY KEY,
    key TEXT NOT NULL, name TEXT, version TEXT,
    definition_digest TEXT NOT NULL, UNIQUE (key, version));
CREATE TABLE run_preset (run_preset_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES run(run_id),
    preset_id TEXT NOT NULL REFERENCES preset(preset_id),
    UNIQUE (run_id, preset_id));
CREATE TABLE raw_record (raw_record_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source(source_id), line_no INTEGER NOT NULL,
    native_record_uid TEXT, recovery_state TEXT,
    raw_json TEXT NOT NULL, UNIQUE (source_id, line_no));

-- Layer 1 ------------------------------------------------------------------
CREATE TABLE activity (activity_id TEXT PRIMARY KEY, kind TEXT, context_json TEXT);
CREATE TABLE activity_record (activity_record_id TEXT PRIMARY KEY,
    activity_id TEXT NOT NULL REFERENCES activity(activity_id),
    raw_record_id TEXT NOT NULL REFERENCES raw_record(raw_record_id),
    UNIQUE (activity_id, raw_record_id));
CREATE TABLE entity (entity_id TEXT PRIMARY KEY,
    identifier TEXT, raw_identifier TEXT, identifier_type TEXT NOT NULL,
    type TEXT NOT NULL, concerns TEXT, concerns_origin TEXT, concerns_uid TEXT);
CREATE INDEX ix_entity_norm ON entity(identifier_type, identifier);
CREATE INDEX ix_entity_concerns ON entity(concerns_uid);
CREATE TABLE spatial_expression (spatial_expression_id TEXT PRIMARY KEY,
    referential TEXT NOT NULL, spatial_source TEXT);
CREATE TABLE spatial_component (spatial_component_id TEXT PRIMARY KEY,
    spatial_expression_id TEXT NOT NULL
        REFERENCES spatial_expression(spatial_expression_id),
    ordinal INTEGER NOT NULL, part TEXT NOT NULL, spatial_role TEXT,
    source_field TEXT, raw_value TEXT,
    UNIQUE (spatial_expression_id, ordinal));
CREATE TABLE temporal_expression (temporal_expression_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL, referential TEXT, temporal_source TEXT);
CREATE TABLE temporal_component (temporal_component_id TEXT PRIMARY KEY,
    temporal_expression_id TEXT NOT NULL
        REFERENCES temporal_expression(temporal_expression_id),
    ordinal INTEGER NOT NULL, part TEXT NOT NULL, temporal_role TEXT,
    stated_offset TEXT, source_field TEXT, raw_value TEXT,
    UNIQUE (temporal_expression_id, ordinal));
CREATE TABLE assertion (assertion_id TEXT PRIMARY KEY,
    raw_record_id TEXT NOT NULL REFERENCES raw_record(raw_record_id),
    preset_id TEXT NOT NULL REFERENCES preset(preset_id),
    output_ordinal INTEGER NOT NULL,
    entity_id TEXT NOT NULL UNIQUE REFERENCES entity(entity_id),
    entity_origin TEXT,
    activity_id TEXT REFERENCES activity(activity_id),
    spatial_expression_id TEXT UNIQUE
        REFERENCES spatial_expression(spatial_expression_id),
    temporal_expression_id TEXT UNIQUE
        REFERENCES temporal_expression(temporal_expression_id),
    entity_role TEXT, entity_position_link TEXT, entity_time_link TEXT,
    spatial_temporal_link TEXT, deleted TEXT, multiplicity INTEGER,
    source_label TEXT, input_record_id TEXT,
    content_hash TEXT NOT NULL, claim_digest TEXT NOT NULL,
    UNIQUE (raw_record_id, preset_id, output_ordinal));
CREATE INDEX ix_assertion_ch ON assertion(content_hash);
CREATE INDEX ix_assertion_cd ON assertion(claim_digest);
CREATE INDEX ix_assertion_act ON assertion(activity_id);

-- Layer 2 ------------------------------------------------------------------
CREATE TABLE spatial_integration (spatial_integration_id TEXT PRIMARY KEY,
    spatial_expression_id TEXT NOT NULL UNIQUE
        REFERENCES spatial_expression(spatial_expression_id),
    method TEXT NOT NULL,
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
    method TEXT NOT NULL,
    lower_unix_utc_us INTEGER, upper_unix_utc_us INTEGER,
    lower_datetime_iso TEXT, upper_datetime_iso TEXT,
    lower_date INTEGER, upper_date INTEGER, lower_time INTEGER, upper_time INTEGER,
    utc_offset_hours_lower TEXT, utc_offset_hours_upper TEXT, time_accuracy_us INTEGER);

-- Layer 3 ------------------------------------------------------------------
CREATE TABLE analysis (analysis_id TEXT PRIMARY KEY,
    assertion_id TEXT NOT NULL REFERENCES assertion(assertion_id),
    method TEXT NOT NULL, record_type TEXT, record_rank INTEGER,
    UNIQUE (assertion_id, method));
CREATE TABLE edit_log (edit_id TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL UNIQUE,
    table_name TEXT, row_id TEXT, column TEXT, old_value TEXT, new_value TEXT,
    edit_kind TEXT, edited_by TEXT, edited_at TEXT);

-- Findings are VIEWS, never merges (schema §Silent-loss inventory).
CREATE VIEW duplicate_sources AS
    SELECT fingerprint_sha256, COUNT(*) AS copies,
           GROUP_CONCAT(input_file_path, ' | ') AS files
    FROM source GROUP BY fingerprint_sha256 HAVING COUNT(*) > 1;
CREATE VIEW possible_same_evidence AS
    SELECT a.claim_digest, COUNT(*) AS copies,
           GROUP_CONCAT(s.input_file_path, ' | ') AS files
    FROM assertion a
    JOIN raw_record r ON r.raw_record_id = a.raw_record_id
    JOIN source s ON s.source_id = r.source_id
    GROUP BY a.claim_digest HAVING COUNT(*) > 1;
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
    ingest write order: run -> source -> raw_record -> activity(+link) -> entity +
    expressions + components -> assertion -> integrations -> analysis. No
    ``INSERT OR IGNORE`` anywhere: a UNIQUE violation here would be a derivation bug and
    must raise (schema §Ingestion write order).
    """

    def __init__(self, cx: sqlite3.Connection, case_id: str, run_id: str):
        self.cx = cx
        self.case_id = case_id
        self.run_id = run_id
        self._presets: dict[tuple[str, str], str] = {}

    # ---- Layer 0 ---------------------------------------------------------
    def preset(self, key: str, name: str, version: str) -> str:
        # Content-keyed (D27): a preset's definition IS its identity. D40 adds a
        # definition_digest beside the version to catch un-bumped edits; the real
        # pipeline hashes the canonical preset YAML — the demo hashes a stub.
        pid = u5("preset", key, version)
        if (key, version) not in self._presets:
            dd = hashlib.sha256(
                json.dumps({"key": key, "name": name, "version": version},
                           sort_keys=True).encode()).hexdigest()
            self.cx.execute("INSERT INTO preset VALUES (?,?,?,?,?)",
                            (pid, key, name, version, dd))
            # D39: the run records every preset selected for it.
            self.cx.execute("INSERT INTO run_preset VALUES (?,?,?)",
                            (u5("run_preset", self.run_id, pid), self.run_id, pid))
            self._presets[(key, version)] = pid
        return pid

    def source(self, path, inner, data_source, fmt, subunit, fp, exhibit=None,
               tier="primary", siblings=None):
        # id from PATHS, never the fingerprint (§2c fix): two byte-identical exhibits
        # stay two sources whose matching hash is a finding, not a merge.
        sid = u5("source", path, inner, subunit)
        self.cx.execute("INSERT INTO source VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, self.run_id, path, inner, data_source, fmt, subunit, fp,
             json.dumps(siblings or []), exhibit, tier, None))
        return sid

    def archive(self, path, cd_sha256, skipped=0):
        # D38: id from the path only (2c - a content hash is never a key); the
        # run link is a row in run_archive (one run, many archives; one archive,
        # many runs), so every encounter - including an opt-in skip - is recorded.
        aid = u5("archive", path)
        self.cx.execute("INSERT INTO archive VALUES (?,?,?)", (aid, path, cd_sha256))
        self.cx.execute("INSERT INTO run_archive VALUES (?,?,?,?,?)",
            (u5("run_archive", self.run_id, aid), self.run_id, aid,
             "2026-03-15T08:00:05Z", skipped))
        return aid

    def record(self, sid, line, raw: dict, recovery="live"):
        rrid = u5("record", sid, line)
        # raw_json is the COMPLETE row (verbatim values).
        self.cx.execute("INSERT INTO raw_record VALUES (?,?,?,?,?,?)",
            (rrid, sid, line, None, recovery, canonical_json(raw)))
        return rrid

    # ---- Layer 1 ---------------------------------------------------------
    def activity(self, rrid, kind, context=None, ordinal=0):
        aid = u5("activity", rrid, ordinal)
        self.cx.execute("INSERT INTO activity VALUES (?,?,?)",
            (aid, kind, None if context is None else canonical_json(context)))
        # exactly one link per activity at ingest — its own record (D26/R7 invariant).
        self.cx.execute("INSERT INTO activity_record VALUES (?,?,?)",
            (u5("actrec", aid, rrid), aid, rrid))
        return aid, context

    def entity(self, rrid, ordinal, identifier, raw_identifier, id_type, etype,
               concerns=None, concerns_origin=None):
        eid = u5("entity", rrid, ordinal, identifier, id_type, etype)
        concerns_uid = None
        if concerns is not None:
            # D22: case-folded/stemmed digest so "A. Moreau"/"moreau a." group together,
            # derived, never a key, excluded from the evidence digests.
            folded = " ".join(sorted(concerns.lower().replace(".", " ").split()))
            concerns_uid = sha256_hex(folded)[:16]
        self.cx.execute("INSERT INTO entity VALUES (?,?,?,?,?,?,?,?)",
            (eid, identifier, raw_identifier, id_type, etype,
             concerns, concerns_origin, concerns_uid))
        return eid, {"identifier": identifier, "identifier_type": id_type, "type": etype}

    def spatial(self, rrid, ordinal, referential, spatial_source, components,
                method, lat, lon, **metrics):
        """components: list of (part, spatial_role, source_field, raw_value)."""
        sx = u5("sx", rrid, ordinal)
        self.cx.execute("INSERT INTO spatial_expression VALUES (?,?,?)",
            (sx, referential, spatial_source))
        comp_dicts = []
        for i, (part, role, field, rawv) in enumerate(components):
            self.cx.execute("INSERT INTO spatial_component VALUES (?,?,?,?,?,?,?)",
                (u5("sc", sx, i), sx, i, part, role, field, rawv))
            comp_dicts.append({"part": part, "spatial_role": role, "raw_value": rawv})
        if lat is not None:
            si = u5("si", sx, method)
            self.cx.execute("INSERT INTO spatial_integration VALUES "
                            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (si, sx, method, lat, lon,
                 metrics.get("altitude_m"), metrics.get("horizontal_accuracy_m"),
                 metrics.get("vertical_accuracy_m"), metrics.get("horizontal_speed_kmh"),
                 metrics.get("vertical_speed_kmh"),
                 metrics.get("horizontal_speed_accuracy_kmh"),
                 metrics.get("vertical_speed_accuracy_kmh"),
                 metrics.get("heading_deg"), metrics.get("heading_accuracy_deg"),
                 metrics.get("beam_azimuth_deg"), metrics.get("beam_width_deg")))
            # D31: one S2 leaf cell per point, indexed (non-unique s2_cell_id).
            self.cx.execute("INSERT INTO spatial_index VALUES (?,?,?,?)",
                (u5("s2", si), si, s2_signed_leaf(lat, lon), S2_LEAF_LEVEL))
        # ordinal ordering matters for the L1 projection, so hand it back ready-sorted.
        return sx, {"referential": referential, "spatial_source": spatial_source,
                    "components": comp_dicts}

    def temporal(self, rrid, ordinal, kind, referential, temporal_source, components,
                 method, lower_us, upper_us, off_lower=None, off_upper=None,
                 time_accuracy_us=None):
        """components: list of (part, temporal_role, stated_offset, source_field, raw)."""
        tx = u5("tx", rrid, ordinal)
        self.cx.execute("INSERT INTO temporal_expression VALUES (?,?,?,?)",
            (tx, kind, referential, temporal_source))
        comp_dicts = []
        for i, (part, role, stated, field, rawv) in enumerate(components):
            self.cx.execute("INSERT INTO temporal_component VALUES (?,?,?,?,?,?,?,?)",
                (u5("tc", tx, i), tx, i, part, role, stated, field, rawv))
            comp_dicts.append({"part": part, "temporal_role": role,
                               "stated_offset": stated, "raw_value": rawv})
        lo_iso, lo_date, lo_time = local_projection(lower_us, off_lower)
        up_iso, up_date, up_time = local_projection(upper_us, off_upper)
        self.cx.execute("INSERT INTO timespan_integration VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (u5("ti", tx, method), tx, method, lower_us, upper_us,
             lo_iso, up_iso, lo_date, up_date, lo_time, up_time,
             _off_text(off_lower), _off_text(off_upper), time_accuracy_us))
        return tx, {"kind": kind, "referential": referential, "components": comp_dicts}

    def assertion(self, rrid, preset_id, preset_key, ordinal, entity_pair,
                  spatial_pair, temporal_pair, activity_pair, role, epl, etl, stl,
                  deleted=None, multiplicity=None, label=None, input_record_id=None,
                  entity_origin="preset_mapped"):
        eid, entity_l1 = entity_pair
        sx, spatial_l1 = spatial_pair or (None, None)
        tx, temporal_l1 = temporal_pair or (None, None)
        act_id, act_ctx = activity_pair or (None, None)
        links = {"entity_role": role, "entity_position_link": epl,
                 "entity_time_link": etl, "spatial_temporal_link": stl}
        activity_l1 = None if act_id is None else {"kind": _activity_kind(self.cx, act_id),
                                                   "context_json": act_ctx}
        l1 = l1_projection(entity=entity_l1, spatial=spatial_l1, temporal=temporal_l1,
                           links=links, deleted=deleted, multiplicity=multiplicity,
                           activity=activity_l1)
        # claim_digest (R5) = the L1 projection ALONE — preset/file-independent, so two
        # presets extracting the same claim converge and surface as possible-same-evidence.
        claim_digest = sha256_hex(l1)
        fp, line_no = self.cx.execute(
            "SELECT s.fingerprint_sha256, r.line_no FROM raw_record r "
            "JOIN source s ON s.source_id=r.source_id WHERE r.raw_record_id=?",
            (rrid,)).fetchone()
        # content_hash = fingerprint ⟂ preset key ⟂ line_no ⟂ output_ordinal ⟂ L1 —
        # the digest scoped to THIS preset's reading of THIS file (D27); no paths, so
        # folder==zip parity holds.
        content_hash = sha256_hex(SEP.join(
            [_txt(fp), preset_key, str(line_no), str(ordinal), l1]))
        aid = u5("assertion", rrid, preset_key, ordinal)
        self.cx.execute("INSERT INTO assertion VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, rrid, preset_id, ordinal, eid, entity_origin, act_id, sx, tx,
             role, epl, etl, stl, deleted, multiplicity, label, input_record_id,
             content_hash, claim_digest))
        return aid

    def analysis(self, aid, method, record_type, record_rank):
        self.cx.execute("INSERT INTO analysis VALUES (?,?,?,?,?)",
            (u5("analysis", aid, method), aid, method, record_type, record_rank))


def _off_text(off: float | str | None) -> str | None:
    # Applied offset per bound: numeric hours as text, or the "N/A" assumed-UTC sentinel,
    # or NULL when there is no bound at all.
    if off is None:
        return None
    return off if isinstance(off, str) else f"{float(off):+.1f}"


def _activity_kind(cx: sqlite3.Connection, activity_id: str) -> str:
    return cx.execute("SELECT kind FROM activity WHERE activity_id=?",
                      (activity_id,)).fetchone()[0]


# --------------------------------------------------------------------------- #
# The worked example — one night in Geneva                                     #
# --------------------------------------------------------------------------- #

def build() -> sqlite3.Connection:
    if DB.exists():
        DB.unlink()
    cx = sqlite3.connect(DB)
    cx.executescript(DDL)

    # case + run: the only uuid4 rows (D28/D10); everything downstream is uuid5.
    case_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    cx.execute("INSERT INTO case_metadata VALUES (?,?,?,?,?)",
        (case_id, "GE-2026-04412", "Rue du Stand robbery",
         "One-night Geneva investigation — worked-example fixture", "2026-03-15T08:00:00Z"))
    cx.execute("INSERT INTO run VALUES (?,?,?,?,?,?,?,?)",
        (run_id, case_id, "/case", "2026-03-15T08:00:00Z", "2026-03-15T08:04:12Z",
         "matlas/example-0", "{}", "insp. Girard"))
    cx.execute("INSERT INTO matlas_meta VALUES (?,?,?,?)",
        ("example-0", "example-0", "norm_rules/1.0", "stem_rules/1.0"))

    b = Builder(cx, case_id, run_id)

    # Presets (D27 — companion table; one per reading applied in this run).
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
    # WHY twice: folder==zip must produce equal content_hash/claim_digest (no paths in
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
            sx = b.spatial(rr, 0, "epsg:4326", "GNSS",
                [("latitude_wgs84", None, "ZLATITUDE", lat),
                 ("longitude_wgs84", None, "ZLONGITUDE", lon),
                 ("horizontal_accuracy_m", None, "ZHORIZONTALACCURACY", acc),
                 ("horizontal_speed_kmh", None, "ZSPEED", sp),
                 ("heading_deg", None, "ZCOURSE", hd)],
                "passthrough_wgs84/1.0+unit_convert/1.0", float(lat), float(lon),
                horizontal_accuracy_m=float(acc),
                horizontal_speed_kmh=round(float(sp) * 3.6, 2), heading_deg=float(hd))
            tx = b.temporal(rr, 0, "instant", "apple_cfabsolute", "internal_clock",
                [("lower", "last_seen", None, "ZTIMESTAMP", cf)],
                "epoch_cfabsolute/1.0", us_from_iso(iso), us_from_iso(iso),
                off_lower="N/A", off_upper="N/A")
            a = b.assertion(rr, p_cache, "ios_cache", 0, ent, sx, tx, None,
                "subject", "at", "observed_at", "instant", label="iOS cache")
            b.analysis(a, "untangle/2.0", "gps_fix", 1)

    # ---- Visit with entry & exit (Local.sqlite) --------------------------
    sid = b.source("/case/exhibit_A/local.sqlite", None, None, "sqlite",
                   "ZRTVISITMO", "fp-local-7b02", "EX-A/1")
    rr = b.record(sid, 1, {"ZLAT": "46.193000", "ZLON": "6.129000",
                           "ZENTRYDATE": "763689600", "ZEXITDATE": "763702200"})
    ent = b.entity(rr, 0, None, None, "none", "device")
    sx = b.spatial(rr, 0, "epsg:4326", "GNSS",
        [("latitude_wgs84", None, "ZLAT", "46.193000"),
         ("longitude_wgs84", None, "ZLON", "6.129000")],
        "passthrough_wgs84/1.0+unit_convert/1.0", HOME[0], HOME[1],
        horizontal_accuracy_m=40.0)
    # Each bound carries its OWN role (D25): lower=check_in, upper=check_out. An epoch is
    # absolute UTC — the source states no zone, so stated_offset is None ('(none)').
    tx = b.temporal(rr, 0, "interval", "apple_cfabsolute", "internal_clock",
        [("lower", "check_in", None, "ZENTRYDATE", "763689600"),
         ("upper", "check_out", None, "ZEXITDATE", "763702200")],
        "epoch_cfabsolute/1.0", us_from_iso("2026-03-13 20:00:00+01"),
        us_from_iso("2026-03-13 23:30:00+01"), off_lower="N/A", off_upper="N/A")
    b.assertion(rr, p_visit, "ios_visits", 0, ent, sx, tx, None,
        "subject", "at", "observed_at", "continuous_during_interval", label="iOS visits")

    # ---- CDRs: operator A file (3 records; #2 is THE call near the scene) -
    def cdr(sid, line, a_msisdn, b_msisdn, imei, start_iso, dur, cell, coords,
            preset_id, preset_key, azimuth=120, beam_width=65):
        rr = b.record(sid, line, {"a_party": a_msisdn, "b_party": b_msisdn,
            "imei": imei, "start": start_iso[:-3], "duration": str(dur), "cell": cell})
        # D18: the callee + the caller's own IMEI are activity context, NOT parties —
        # they live in context_json (inside the L1 projection), no assertion_mention.
        act = b.activity(rr, "call",
            {"callee": b_msisdn.replace(" ", ""), "caller_imei": imei})
        ent = b.entity(rr, 0, a_msisdn.replace(" ", ""), a_msisdn, "msisdn", "device")
        sx = b.spatial(rr, 0, "cell_ref", "cell",
            [("raw_position", "vicinity", "cell", cell)],
            "cell_register/2025-03", coords[0], coords[1],
            beam_azimuth_deg=float(azimuth), beam_width_deg=float(beam_width))
        lo = us_from_iso(start_iso)
        tx = b.temporal(rr, 0, "interval", "local_string", "NTP",
            [("lower", "start", "+01:00", "start", start_iso[:-3]),
             ("duration", "end", "+01:00", "duration", str(dur))],
            "tz_localize/tzdb2026a+start_plus_duration/1.0",
            lo, lo + dur * 1_000_000, off_lower=1.0, off_upper=1.0)
        a = b.assertion(rr, preset_id, preset_key, 0, ent, sx, tx, act,
            "caller", "within_range_of", "event_at", "continuous_during_interval",
            label="CDR op-A")
        b.analysis(a, "untangle/2.0", "cdr_leg", 1)
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
    act = b.activity(rr, "call", {"counterpart": "+41791234567"})
    ent = b.entity(rr, 0, "+41762223344", "+41762223344", "msisdn", "device")
    sx = b.spatial(rr, 0, "cell_ref", "cell",
        [("raw_position", "vicinity", "cell_b", "228-02-1011-55221")],
        "cell_register/2025-03", CELL_B[0], CELL_B[1],
        beam_azimuth_deg=300.0, beam_width_deg=65.0)
    lo = us_from_iso("2026-03-14 02:20:31+01")
    tx = b.temporal(rr, 0, "interval", "local_string", "NTP",
        [("lower", "start", "+01:00", "start", "2026-03-14 02:20:31"),
         ("duration", "end", "+01:00", "duration", "187")],
        "tz_localize/tzdb2026a+start_plus_duration/1.0",
        lo, lo + 187_000_000, off_lower=1.0, off_upper=1.0)
    b.assertion(rr, p_cdr, "cdr_generic", 0, ent, sx, tx, act,
        "callee", "within_range_of", "event_at", "continuous_during_interval",
        label="CDR op-B")

    # ---- overlapping disclosure: operator A supplement re-contains #2 -----
    # WHY: same claim in a different file -> equal claim_digest, distinct content_hash
    # (different fingerprint) — a possible-same-evidence finding, never a merge.
    sid = b.source("/case/operatorA_cdr_supplement.csv", None, None, "csv", None,
                   "fp-cdrA2-33cc", "REQ-77bis")
    cdr(sid, 1, "+41 79 123 45 67", "+41 76 222 33 44", "352099001761481",
        "2026-03-14 02:20:31+01", 187, "228-01-2042-77341", CELL_A, p_cdr, "cdr_generic")

    # ---- ANPR: three reads of the suspect's plate ------------------------
    # The camera IS the spatial expression (D18): its id is the cell_ref/asset_ref
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
        sx = b.spatial(rr, 0, "cell_ref", "camera",
            [("raw_position", "vicinity", "camera_id", cam)],
            "camera_register/2025-11", coords[0], coords[1])
        tx = b.temporal(rr, 0, "instant", "local_string", "NTP",
            [("lower", "last_seen", "+01:00", "ts", ts[:-3])],
            "tz_stated_offset/1.0", us_from_iso(ts), us_from_iso(ts),
            off_lower=1.0, off_upper=1.0)
        a = b.assertion(rr, p_anpr, "anpr_ch", 0, ent, sx, tx, None,
            "subject", "at_fixed_detector", "observed_at", "instant", label="ANPR")
        b.analysis(a, "untangle/2.0", "anpr_read", 1)

    # ---- CCTV at the scene -----------------------------------------------
    sid = b.source("/case/dvr_index.csv", None, None, "csv", None,
                   "fp-dvr-55ee", "REQ-91")
    rr = b.record(sid, 1, {"channel": "Lobby-2", "site": "Rue du Stand 15, 1204 Geneve",
        "start": "2026-03-14 02:25:00", "end": "2026-03-14 02:50:00"})
    ent = b.entity(rr, 0, "Lobby-2", "Lobby-2", "camera", "camera")
    sx = b.spatial(rr, 0, "address", "manual",
        [("raw_position", "vicinity", "site", "Rue du Stand 15, 1204 Geneve")],
        "geocode_nominatim/2026-06", SCENE[0], SCENE[1])
    tx = b.temporal(rr, 0, "interval", "local_string", "internal_clock",
        [("lower", "start", None, "start", "2026-03-14 02:25:00"),
         ("upper", "end", None, "end", "2026-03-14 02:50:00")],
        "tz_localize/tzdb2026a", us_from_iso("2026-03-14 02:25:00+01"),
        us_from_iso("2026-03-14 02:50:00+01"), off_lower=1.0, off_upper=1.0)
    b.assertion(rr, p_dvr, "dvr_generic", 0, ent, sx, tx, None,
        "subject", "at", "recorded_at", "continuous_during_interval", label="CCTV")

    # ---- robbery in the police register ----------------------------------
    # D36: the event IS the entity (identifier == concerns, identifier_type=case_ref);
    # the victim and the offence stay verbatim in raw_json -> orig_. No activity row:
    # a single placement has nothing to group (D35 - activity is a grouping bonus).
    sid = b.source("/case/police_register.csv", None, None, "csv", None,
                   "fp-pol-66ff", "REQ-92", tier="secondary")
    rr = b.record(sid, 1, {"case": "GE-2026-04412", "offense": "robbery",
        "location": "Rue du Stand 15, Geneve", "from": "2026-03-14 02:30",
        "to": "2026-03-14 02:45", "victim": "M. Keller"})
    ent = b.entity(rr, 0, "GE-2026-04412", "GE-2026-04412", "case_ref", "event",
                   concerns="GE-2026-04412", concerns_origin="preset_mapped")
    sx = b.spatial(rr, 0, "address", "manual",
        [("raw_position", "vicinity", "location", "Rue du Stand 15, Geneve")],
        "geocode_nominatim/2026-06", SCENE[0], SCENE[1])
    tx = b.temporal(rr, 0, "interval", "local_string", "manual",
        [("lower", "start", None, "from", "2026-03-14 02:30"),
         ("upper", "end", None, "to", "2026-03-14 02:45")],
        "tz_localize/tzdb2026a", us_from_iso("2026-03-14 02:30:00+01"),
        us_from_iso("2026-03-14 02:45:00+01"), off_lower=1.0, off_upper=1.0)
    b.assertion(rr, p_police, "police_reg", 0, ent, sx, tx, None,
        "subject", "claimed_at", "reported_for", "continuous_during_interval",
        label="police register")

    # ---- witness audition ------------------------------------------------
    sid = b.source("/case/audition_pv.csv", None, None, "csv", None,
                   "fp-wit-7700", "REQ-93", tier="secondary")
    rr = b.record(sid, 1, {"witness": "Marie Dupont",
        "statement": "J'ai vu l'homme pres de la gare vers midi le 14 mars"})
    # concerns = the reporter (Marie Dupont), fixed at parsing (D17 absorbs linked_entity).
    ent = b.entity(rr, 0, None, "l'homme", "none", "person",
                   concerns="Marie Dupont", concerns_origin="preset_mapped")
    sx = b.spatial(rr, 0, "place_name", "manual",
        [("raw_position", "vicinity", "statement", "pres de la gare")],
        "place_centroid/geocoder-x", STATION[0], STATION[1])
    tx = b.temporal(rr, 0, "interval", "local_string", "manual",
        [("lower", "start", "+01:00", "statement", "vers midi le 14 mars"),
         ("upper", "end", "+01:00", "statement", "vers midi le 14 mars")],
        "fuzzy_time/1.0", us_from_iso("2026-03-14 11:00:00+01"),
        us_from_iso("2026-03-14 13:00:00+01"), off_lower=1.0, off_upper=1.0,
        time_accuracy_us=3600_000_000)
    b.assertion(rr, p_audit, "audition", 0, ent, sx, tx, None,
        "reporter", "claimed_at", "reported_for", "continuous_during_interval",
        label="audition")

    # ---- shared pin in instant messaging (REFERENCED, never presence) ----
    sid = b.source("/case/exhibit_A/chat.db", None, None, "sqlite", "messages",
                   "fp-chat-8811", "EX-A/1")
    rr = b.record(sid, 1, {"sender": "+41791234567", "recipient": "+41762223344",
        "ts": "1773450680", "lat": "46.203000", "lon": "6.140000",
        "caption": "meet me here", "is_deleted": "1"})
    act = b.activity(rr, "message", {"recipient": "+41762223344"})
    ent = b.entity(rr, 0, "+41791234567", "+41791234567", "msisdn", "device")
    sx = b.spatial(rr, 0, "epsg:4326", "GNSS",
        [("latitude_wgs84", None, "lat", "46.203000"),
         ("longitude_wgs84", None, "lon", "6.140000")],
        "passthrough_wgs84/1.0", SCENE[0], SCENE[1])
    tx = b.temporal(rr, 0, "instant", "unix_epoch_s", "NTP",
        [("lower", "start", None, "ts", "1773450680")],   # epoch: no stated zone
        "epoch_unix_s/1.0", 1773450680_000_000, 1773450680_000_000,
        off_lower="N/A", off_upper="N/A")
    # deleted = the APP's own flag (R11) — a different fact from raw_record.recovery_state.
    b.assertion(rr, p_im, "im_generic", 0, ent, sx, tx, act,
        "sender", "references", "event_at", "instant",
        deleted="deleted", label="chat pin")

    # ---- route navigation search — TWO places (origin, destination) ------
    sid = b.source("/case/exhibit_A/maps_history.db", None, None, "sqlite",
                   "searches", "fp-maps-9922", "EX-A/1")
    rr = b.record(sid, 1, {"account": "jdoe@gmail.com",
        "query_ts": "2026-03-13T21:04:09Z", "origin": "Home",
        "destination": "Rue du Stand 15, Geneve"})
    act = b.activity(rr, "trip", {"origin": "Home", "destination": "Rue du Stand 15, Geneve"})
    for ordinal, (ref, role, rawv, geo) in enumerate((
            ("address", "destination", "Rue du Stand 15, Geneve", SCENE),
            ("place_name", "origin", "Home", None))):
        ent = b.entity(rr, ordinal, "jdoe@gmail.com", "jdoe@gmail.com", "email", "account")
        sx = b.spatial(rr, ordinal, ref, "manual",
            [("raw_position", role, role, rawv)],
            "geocode_nominatim/2026-06", *(geo or (None, None)))
        tx = b.temporal(rr, ordinal, "instant", "iso8601", "NTP",
            [("lower", "start", "+00:00", "query_ts", "2026-03-13T21:04:09Z")],
            "passthrough_utc/1.0", us_from_iso("2026-03-13 21:04:09+00"),
            us_from_iso("2026-03-13 21:04:09+00"), off_lower=0.0, off_upper=0.0)
        b.assertion(rr, p_nav, "nav_generic", ordinal, ent, sx, tx, act,
            "subject", "references", "event_at", "instant", label="nav search")

    # ---- an explicit alibi: NEVER_DURING_INTERVAL (negated presence) -----
    # WHY: gives presence queries something to EXCLUDE — the suspect's phone was NOT at
    # home during the robbery window (a negated claim; the graph layer is deferred, D16).
    sid = b.source("/case/alibi_statement.csv", None, None, "csv", None,
                   "fp-alibi-bb44", "REQ-94", tier="secondary")
    rr = b.record(sid, 1, {"subject": "+41791234567", "place": "Home",
        "from": "2026-03-14 02:30", "to": "2026-03-14 02:45",
        "claim": "not present"})
    ent = b.entity(rr, 0, "+41791234567", "+41791234567", "msisdn", "device")
    sx = b.spatial(rr, 0, "place_name", "manual",
        [("raw_position", "vicinity", "place", "Home")],
        "place_centroid/geocoder-x", HOME[0], HOME[1])
    tx = b.temporal(rr, 0, "interval", "local_string", "manual",
        [("lower", "start", "+01:00", "from", "2026-03-14 02:30"),
         ("upper", "end", "+01:00", "to", "2026-03-14 02:45")],
        "tz_localize/tzdb2026a", us_from_iso("2026-03-14 02:30:00+01"),
        us_from_iso("2026-03-14 02:45:00+01"), off_lower=1.0, off_upper=1.0)
    b.assertion(rr, p_audit, "audition", 0, ent, sx, tx, None,
        "subject", "claimed_at", "never_during_interval", "continuous_during_interval",
        label="alibi")

    # ---- one edit_log row (D34/D41): an analyst corrected a geocode in place --
    # Integration rows MAY update in place; the change is audited, evidence untouched.
    # D41: the ordinal makes the journal totally ordered, so replaying inverses in
    # reverse ordinal order rewinds the DB to any earlier state.
    cx.execute("INSERT INTO edit_log VALUES (?,?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), 1, "spatial_integration", "(demo)", "latitude_wgs84",
         "46.2029", "46.2030", "update", "insp. Girard", "2026-03-15T09:12:00Z"))

    cx.commit()
    return cx


# --------------------------------------------------------------------------- #
# Demonstration queries — the Q1..Q10 story, new projection columns           #
# --------------------------------------------------------------------------- #

# The flat projection (csv_migration.md contract): one line per assertion. locates is
# DERIVED from entity_position_link (D11), never stored. Context columns come from
# activity.context_json (D18). datetime_iso columns are D32 local offset-tagged strings.
FLAT = """
SELECT substr(a.assertion_id,1,8) AS row_uid,
  e.identifier AS entity, e.raw_identifier, e.identifier_type, e.type AS entity_type,
  a.entity_role AS role,
  json_extract(act.context_json,'$.counterpart') AS counterpart,
  json_extract(act.context_json,'$.callee') AS callee,
  json_extract(act.context_json,'$.caller_imei') AS imei,
  sx.referential AS spatial_referential,
  CASE a.entity_position_link
    WHEN 'at' THEN 'self' WHEN 'claimed_at' THEN 'self'
    WHEN 'within_range_of' THEN 'infrastructure'
    WHEN 'at_fixed_detector' THEN 'infrastructure'
    WHEN 'references' THEN 'referenced' ELSE 'unknown' END AS locates,
  si.latitude_wgs84 AS lat, si.longitude_wgs84 AS lon, six.s2_cell_id,
  ti.lower_datetime_iso AS t_lower, ti.upper_datetime_iso AS t_upper,
  a.source_label
FROM assertion a
JOIN entity e ON e.entity_id=a.entity_id
LEFT JOIN activity act ON act.activity_id=a.activity_id
LEFT JOIN spatial_expression sx ON sx.spatial_expression_id=a.spatial_expression_id
LEFT JOIN spatial_integration si ON si.spatial_expression_id=a.spatial_expression_id
LEFT JOIN spatial_index six ON six.spatial_integration_id=si.spatial_integration_id
LEFT JOIN timespan_integration ti ON ti.temporal_expression_id=a.temporal_expression_id
"""

_LO = us_from_iso("2026-03-14 02:00:00+01")
_HI = us_from_iso("2026-03-14 03:10:00+01")

QUERIES = [
 ("Q1 - The flat projection (one line per placement); the whole example run",
  FLAT + "ORDER BY ti.lower_unix_utc_us, a.source_label, row_uid"),

 ("Q2 - Timeline of ONE identifier across every source (convergence by GROUP-able "
  "normalized identifier, no stored merge)",
  FLAT + "WHERE e.identifier_type='msisdn' AND e.identifier='+41791234567' "
         "ORDER BY ti.lower_unix_utc_us, a.source_label, row_uid"),

 ("Q3 - WHO WAS AT THE SCENE 02:00-03:10? Presence only: referenced places AND the "
  "negated alibi excluded by the model itself",
  FLAT + f"""WHERE si.latitude_wgs84 BETWEEN 46.198 AND 46.208
  AND si.longitude_wgs84 BETWEEN 6.133 AND 6.146
  AND ti.lower_unix_utc_us <= {_HI} AND ti.upper_unix_utc_us >= {_LO}
  AND a.entity_position_link != 'references'
  AND a.entity_time_link != 'never_during_interval'
ORDER BY ti.lower_unix_utc_us, a.source_label, row_uid"""),

 ("Q4 - ...and the REFERENCED pointers at the same place (leads, not presence)",
  FLAT + """WHERE si.latitude_wgs84 BETWEEN 46.198 AND 46.208
  AND si.longitude_wgs84 BETWEEN 6.133 AND 6.146
  AND a.entity_position_link = 'references'
ORDER BY row_uid"""),

 ("Q5 - Duplicate evidence findings (never merged, always reported; duplicates SURFACE)",
  "SELECT 'same file bytes' AS finding, fingerprint_sha256 AS digest, copies, files "
  "FROM duplicate_sources "
  "UNION ALL "
  "SELECT 'possible same evidence (file-independent)', substr(claim_digest,1,16), "
  "copies, files FROM possible_same_evidence ORDER BY finding, digest"),

 ("Q6 - Convergence by normalized identifier: every mention of +41791234567, with its "
  "VERBATIM raw_identifier surfaced (D17/D21 — same working id, different source text)",
  """SELECT e.identifier, e.raw_identifier, e.identifier_type, e.type,
     a.entity_role, a.source_label
FROM assertion a JOIN entity e ON e.entity_id=a.entity_id
WHERE e.identifier_type='msisdn' AND e.identifier='+41791234567'
ORDER BY a.source_label, e.raw_identifier"""),

 ("Q7 - Which identifiers used tower 228-01-2042-77341? (infrastructure archaeology; "
  "duplicates surface, no DISTINCT)",
  """SELECT e.identifier AS entity, a.entity_role,
     ti.lower_datetime_iso AS at_local, a.source_label, s.input_file_path
FROM spatial_component sc
JOIN spatial_expression sx ON sx.spatial_expression_id=sc.spatial_expression_id
JOIN assertion a ON a.spatial_expression_id=sx.spatial_expression_id
JOIN entity e ON e.entity_id=a.entity_id
JOIN timespan_integration ti ON ti.temporal_expression_id=a.temporal_expression_id
JOIN raw_record r ON r.raw_record_id=a.raw_record_id
JOIN source s ON s.source_id=r.source_id
WHERE sc.raw_value='228-01-2042-77341'
ORDER BY ti.lower_unix_utc_us, s.input_file_path"""),

 ("Q8 - Trace one CSV value back to the source (the 02:20 tower position), through the "
  "expression/component/integration/S2 chain",
  """SELECT si.method, si.latitude_wgs84, si.longitude_wgs84, six.s2_cell_id, six.s2_level,
     sc.part, sc.spatial_role, sc.source_field, sc.raw_value AS verbatim,
     r.line_no, s.input_file_path
FROM spatial_integration si
JOIN spatial_expression sx ON sx.spatial_expression_id=si.spatial_expression_id
JOIN spatial_component sc ON sc.spatial_expression_id=sx.spatial_expression_id
JOIN spatial_index six ON six.spatial_integration_id=si.spatial_integration_id
JOIN assertion a ON a.spatial_expression_id=sx.spatial_expression_id
JOIN raw_record r ON r.raw_record_id=a.raw_record_id
JOIN source s ON s.source_id=r.source_id
WHERE sc.raw_value='228-01-2042-77341'
  AND s.input_file_path LIKE '%operatorA_cdr.csv'"""),

 ("Q9 - The two per-bound temporal projections (D32): stated offset (L1) vs applied "
  "offset + local datetime_iso (L2), plus the ` N/A` assumed-UTC sentinel",
  """SELECT a.source_label, tc.part, tc.temporal_role, tc.stated_offset,
     ti.utc_offset_hours_lower AS applied_lower, ti.utc_offset_hours_upper AS applied_upper,
     ti.lower_datetime_iso, ti.upper_datetime_iso
FROM assertion a
JOIN temporal_expression tx ON tx.temporal_expression_id=a.temporal_expression_id
JOIN temporal_component tc ON tc.temporal_expression_id=tx.temporal_expression_id
JOIN timespan_integration ti ON ti.temporal_expression_id=tx.temporal_expression_id
WHERE a.source_label IN ('iOS cache','CDR op-A','chat pin')
ORDER BY a.source_label, tc.ordinal
LIMIT 12"""),

 ("Q10 - The analysis layer: the current ranker's verdict per assertion (re-running a "
  "changed ranker ADDS rows, never mutates; the CSV projects the current method)",
  """SELECT an.method, an.record_type, an.record_rank, e.type AS entity_type,
     a.source_label, COUNT(*) AS n
FROM analysis an
JOIN assertion a ON a.assertion_id=an.assertion_id
JOIN entity e ON e.entity_id=a.entity_id
GROUP BY an.method, an.record_type, an.record_rank, e.type, a.source_label
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


if __name__ == "__main__":
    cx = build()
    # A diagnostic to stderr; the queries table on stdout is the deliverable (a result,
    # not a log — so print() is correct there per the logging-over-print exception).
    print(f"built {DB}", file=sys.stderr)
    if "--queries" in sys.argv:
        print_queries(cx)
    cx.close()
