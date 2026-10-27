"""The L1 writer: records × one matched preset → observations / expressions / components.

Defines:    ``assemble_source`` (the fan-out engine: one reader record × one matched
            preset → entity, spatial/temporal expressions with their
            verbatim component rows, and the observation tying them together, written into
            a ``core.store.RunStore``), ``AssembleReport`` (its testable summary, carrying
            the source-grain ``reported_data_source`` for the caller), and the
            EXTENDED preset field vocabulary (``identifier_type``, the two referentials,
            the per-bound temporal role/part names, ``data_source``) that maps the new
            L1 columns the old flat model never had.
Used by:    the app-layer pipeline (``model_atlas.app.pipeline``) and
            tests/ingest/test_assemble.py.
Depends on: ``model_atlas.core`` (``ids`` — every id and both digests, ``vocabularies``,
            ``store``), ``model_atlas.ingest.readers.base`` (the reader ``RawRecord``),
            ``model_atlas.presets`` (``spec``, ``fields``, ``expr`` — the unchanged v3
            preset language; ``pipes`` — ``PipeContext``; ``resolve`` — the pure
            resolver functions, see "Reused resolvers" below).

This is the successor to ``transforms/assemble.py`` (the old module was
deleted at the cut-over to the relational engine). What was ported is the RESOLUTION
machinery (refs, conditionals, templates, params, extract); what is NEW is the
EMISSION: instead of one flat 45-column row per observation, the engine writes the
settled relational L1 shape (docs/run_db_schema.md).

Reused resolvers
-----------------
``is_missing`` / ``resolve_ref`` / ``resolve_field`` / ``resolve_cond`` /
``resolve_param`` / ``preset_values`` are IMPORTED from ``presets.resolve`` (their
settled home) rather than re-ported. WHY: they are pure functions of their arguments
(row dict + parsed refs + an ``entity``/``concerns`` param holder — no store, no
module state), and the conditional / template semantics —
first-match-wins, ``NA_SENTINEL`` on no-match, empty-rendered missing placeholders —
live inside them. Importing keeps them single-sourced; a re-port would create a second
copy that could silently drift. The ``env`` they take is duck-typed here by
``_ParamEnv`` (only ``.entity`` / ``.concerns`` are read).

The old→new field mapping (cross-check table of run_db_schema.md)
-----------------------------------------------------------------
- ``entity``            → ``entity.raw_identifier`` (VERBATIM; the
  normalised ``identifier`` and the definitive ``identifier_type`` are the entity
  normaliser's job: assemble leaves ``identifier`` NULL and ``concerns_uid`` NULL).
- ``entity_type``       → ``entity.type`` (default ``unknown`` — the column is NOT NULL).
- ``concerns``     → ``entity.concerns`` (+ ``concerns_origin``).
- (``position_type`` / ``temporal_role_*`` are GONE: the role they asserted was the preset
  author's opinion, not the source's words. ``source_field`` — the column the value came
  from — carries that meaning objectively, without a closed vocabulary nobody could
  complete. Each spatial component now records its declared ``unit`` instead.)
- ``position_source``   → ``spatial_expression.spatial_source``.
- ``temporal_source``   → ``temporal_expression.temporal_source``.
- ``deleted``           → ``observation.deleted`` (the app's OWN flag, distinct from
  ``raw_record.recovery_state``).
- ``source_label``      → ``observation.source_label`` (the one per-claim provenance field).
- ``raw_source_path``   → the source-grain ``reported_data_source``: assemble never
  emits it per observation — it resolves the value once (first record) and RETURNS it on
  the report so the L0 owner can set it on the ``source`` row (which DROPS it for a
  primary acquisition — see pipeline). ``data_source`` is the
  preferred new spelling; ``raw_source_path`` is accepted as its legacy alias.
- the three ``links``   → ``entity_link`` / ``spatial_link`` / ``temporal_link``; the
  entity link also decides the bearer (decision 55), and on an instant the temporal link is
  written in its one canonical form.
- spatial part fields (``raw_position``, ``latitude``, ``longitude``, … ) → one
  ``spatial_component`` row each, VERBATIM ``raw_value`` + the resolved ``source_field``.
- the time bounds       → ``temporal_component`` rows (parts ``lower``/``upper``, or a
  preset-renamed part such as ``duration``), VERBATIM, with the resolved ``zone`` text
  as each bound's ``stated_offset`` (the old language states one zone per time
  spec, so the same verbatim text is stated on every bound of that expression).

Extended vocabulary — new L1 columns the old flat model never had. These are ordinary
``FieldSpec`` targets resolved through the same machinery (so conditionals
and templates apply to them for free); the v3 YAML loader ADMITS them all (its
``ASSIGNABLE_FIELDS`` gate in ``presets/spec.py`` lists each), so a real YAML preset can map:
``identifier_type``, ``spatial_referential`` /
``temporal_referential`` (explicit override of the derivations below), and
``temporal_part_lower`` /
``temporal_part_upper`` (each bound carries its own role/part).

No conversion at L1 (the run_db_schema.md invariant)
----------------------------------------------------
Component ``raw_value``s and the entity's ``raw_identifier`` are resolved VERBATIM:
conditionals choose a source value and ``extract`` selects source text (still verbatim
characters), but the pipe / unit / cast tail is deliberately NOT applied — every
conversion is Layer-2's job, method-stamped. Scalar observation columns
(links, recovery values…) resolve through the FULL
old tail because a lookup pipe there maps tokens, it does not convert measurements.

No integration is written here (that is Layer 2's job) and nothing is invented: a field
the preset never maps is simply absent; a conditional/template that evaluated but
produced nothing yields the ``N/A`` sentinel, which flows as the TEXT
``N/A`` into free-text columns (a vocabulary CHECK column rejects it LOUDLY — the new
engine has no ``INSERT OR IGNORE``, so nothing is ever dropped silently).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.ids import canonical_json
from model_atlas.core.store import RunStore
from model_atlas.ingest.readers.base import RawRecord
from model_atlas.presets.expr import NA_SENTINEL, parse_param_expr
from model_atlas.presets.fields import make_resolver
from model_atlas.presets.spec import FieldSpec, InputSelector, PresetSpec, TimeSpec
from model_atlas.presets.pipes import PipeContext
from model_atlas.presets.resolve import (
    # The folded pure resolvers (``presets.resolve`` is their settled home); they
    # carry the conditional/template semantics with them by construction. Aliased to
    # the historic private spellings so every call site below reads unchanged.
    is_missing as _is_missing,
    preset_values as _preset_values,
    resolve_cond as _resolve_cond,
    resolve_field as _resolve_field,
    resolve_param as _resolve_param,
    resolve_ref as _resolve_ref,
)

# --- the extended field vocabulary (identifier-string constants) ------------
# One constant per NEW L1 mapping target; suffixed _NAME because each is a field-name
# identifier, not a value. The old names they join are read straight from the resolved
# holder by their historic spelling (entity, entity_type, concerns, position_type,
# position_source, temporal_source, source_label, the three link columns).

IDENTIFIER_TYPE_NAME = "identifier_type"
SPATIAL_REFERENTIAL_NAME = "spatial_referential"
POSITION_TYPE_NAME = "position_type"
TEMPORAL_REFERENTIAL_NAME = "temporal_referential"
TEMPORAL_PART_LOWER_NAME = "temporal_part_lower"
TEMPORAL_PART_UPPER_NAME = "temporal_part_upper"
# The part name an elapsed-span upper bound (``duration_sec:``) is written under. MUST match
# ``integrate.temporal._DURATION_PART_NAME``, the read side — the same string agreement the
# ``_date`` / ``_time`` split parts rely on — and ``V.STANDARD_TEMPORAL_PARTS``, which gates
# export projection.
_DURATION_PART_NAME = "duration"
# Source-stated per-bound timing uncertainty (µs). Written as an OPTIONAL verbatim
# component ``lower_accuracy`` / ``upper_accuracy`` (the side + suffix below) — the suffix
# MUST match ``integrate.temporal._ACCURACY_SUFFIX_NAME``, the read side (string agreement,
# like the ``_date`` / ``_time`` split parts).
TIME_ACCURACY_US_LOWER_NAME = "time_accuracy_us_lower"
TIME_ACCURACY_US_UPPER_NAME = "time_accuracy_us_upper"
_LOWER_SIDE_NAME = "lower"
_UPPER_SIDE_NAME = "upper"
_ACCURACY_PART_SUFFIX = "_accuracy"
DATA_SOURCE_NAME = "data_source"           # source grain, via the report
RAW_SOURCE_PATH_NAME = "raw_source_path"   # legacy alias of data_source

# Old field names read from the resolved holder (their new homes are in the header).
_ENTITY_NAME = "entity"
_ENTITY_TYPE_NAME = "entity_type"
_CONCERNS_NAME = "concerns"
_SPATIAL_SOURCE_NAME = "position_source"
_TEMPORAL_SOURCE_NAME = "temporal_source"
_RECOVERY_STATE_NAME = "recovery_state"
_RECOVERY_SOURCE_NAME = "recovery_source"
_SOURCE_LABEL_NAME = "source_label"
_INPUT_RECORD_ID_NAME = "input_record_id"
# The three stored links, one per arm of the observation (decision 55).
_ENTITY_LINK_NAME = "entity_link"
_SPATIAL_LINK_NAME = "spatial_link"
_TEMPORAL_LINK_NAME = "temporal_link"
# The position BEARER's own mention fields — the entity whose position the spatial arm
# states (a CDR's antenna, an ANPR's camera). Written as a SECOND entity row for `party`, and
# for `mentioned` when the preset maps one (see _bearer_for).
_BEARER_ENTITY_NAME = "bearer_entity"
_BEARER_ENTITY_TYPE_NAME = "bearer_entity_type"
_BEARER_IDENTIFIER_TYPE_NAME = "bearer_identifier_type"

# Recognised time codec for ISO-8601 strings — the same spelling the old engine used.
ISO8601_FORMAT = "iso8601"

# TimeSpec.epoch token → TemporalReferential value. WHY a map and not pass-through: the
# preset epochs are decode instructions (cocoa, webkit, unix_s) while the referential is
# the schema vocabulary; the two spellings differ. unix_ns has no vocabulary member yet,
# so it falls back to "unknown" (extend the vocabulary, a one-place change, when needed).
_EPOCH_REFERENTIAL: dict[str, str] = {
    "unix_s": V.TemporalReferential.UNIX_EPOCH_S.value,
    "unix_ms": V.TemporalReferential.UNIX_EPOCH_MS.value,
    "unix_us": V.TemporalReferential.UNIX_EPOCH_US.value,
    "cocoa": V.TemporalReferential.APPLE_CFABSOLUTE.value,
    "webkit": V.TemporalReferential.WEBKIT.value,
}


@dataclass(frozen=True)
class _ParamEnv:
    """Duck-typed stand-in for the old engine's ``BuildEnv`` in the reused resolvers:
    ``_resolve_ref`` reads exactly ``.entity`` / ``.concerns`` for ``param(...)``
    refs and nothing else, so this narrow holder is the whole contract."""

    entity: str | None = None
    concerns: str | None = None


@dataclass
class AssembleReport:
    """The counts one ``assemble_source`` call produced, plus the source-grain values it
    resolved but does NOT write itself (``reported_data_source`` belongs on the
    ``source`` row, which the L0 writer owns — assemble only reports it)."""

    observation_count: int = 0
    data_source: str | None = None
    # mutable default — a shared [] across instances would be a latent aliasing bug.
    warnings: list[str] = field(default_factory=list)


def _norm(value: Any) -> Any:
    """None for a missing value (None/NaN), the value itself otherwise. The ``N/A``
    sentinel is NOT missing — it is a real produced value and flows (as the TEXT N/A)."""
    return None if _is_missing(value) else value


def _text(value: Any) -> str | None:
    """The VERBATIM text form of a resolved value for a raw_value/stated_offset column:
    None stays None (no row / no offset), everything else is its source text. WHY str()
    here and not at bind time: the digest projection renders the same string, so the
    stored column and the hashed token can never disagree (an int 187 read from SQLite
    and its projected \"187\" must be one spelling)."""
    return None if _is_missing(value) else str(value)


def _resolve_verbatim(
    spec: FieldSpec,
    row: dict[str, Any],
    resolve: Callable[[str | None], str | None],
    file_values: dict[str, Any],
    env: _ParamEnv,
    ctx: PipeContext,
    preset_values: dict[str, Any],
) -> tuple[Any, str | None]:
    """Resolve one field to its VERBATIM L1 value: conditional or plain
    ref, then ``extract`` only.

    HOW: the same head as the old ``_resolve_field`` — a conditional resolves to its
    first-matching branch / else / NA_SENTINEL, a plain ref resolves directly, and an
    ``extract`` pattern selects a substring. The pipe / unit / cast tail is deliberately
    ABSENT. WHY: component raw_values and the entity's raw_identifier are Layer-1
    evidence ("the source's words") — every conversion must be a method-stamped L2 row
    (the run_db_schema.md no-conversion invariant); ``extract`` is allowed because it
    selects verbatim source characters rather than transforming them (the witness
    example's "pres de la gare" is a selection, not a conversion)."""
    if spec.condition is not None:
        value, source_col = _resolve_cond(spec.condition, row, resolve, file_values, env, preset_values)
    else:
        value, source_col = _resolve_ref(spec.ref, row, resolve, file_values, env, preset_values)
    if value is NA_SENTINEL:
        return value, source_col
    if spec.extract is not None and value is not None:
        pattern_name, group = spec.extract
        match = re.search(ctx.patterns[pattern_name], str(value))
        value = match.group(group) if match else None
    return value, source_col


def _apply_extract(
    value: Any, extract: tuple[str, str] | None, ctx: PipeContext) -> Any:
    """Select a substring of a resolved time-bound value (pattern, group), verbatim.

    A time bound (unlike a field) resolves through ``_resolve_ref`` without a pipe tail, so
    its optional ``extract`` is applied here — pulling e.g. "14:00" out of a free-text
    statement. A selection of source characters, so it stays within the L1 no-conversion
    rule; a missing value or no pattern passes straight through, a non-match yields None."""
    if extract is None or _is_missing(value):
        return value
    pattern_name, group = extract
    match = re.search(ctx.patterns[pattern_name], str(value))
    return match.group(group) if match else None


def _temporal_referential(time_spec: TimeSpec, warnings: list[str]) -> str:
    """Derive the temporal referential from the time spec's decode declaration.

    HOW: an ``epoch`` maps through ``_EPOCH_REFERENTIAL``; a ``format`` is ``iso8601``
    or a local wall-clock string. WHY derived from the decode: the decode declaration
    (``epoch:`` / ``format:``) IS the referential information, so deriving it from an
    EXPLICIT instruction keeps presets expressive without a second mandatory field (an
    explicit ``temporal_referential`` mapping still overrides this).
    WHY the no-declaration case WARNS: with neither ``epoch:`` nor ``format:`` the value is
    ASSUMED to already be Unix microseconds — a source that is really seconds / milliseconds /
    an Apple epoch would be silently mis-scaled, so the assumption is SURFACED rather than made
    quietly (mirroring the spatial no-referential warning)."""
    if time_spec.epoch is not None:
        return _EPOCH_REFERENTIAL.get(time_spec.epoch, V.TemporalReferential.UNKNOWN.value)
    if time_spec.format is not None:
        if time_spec.format == ISO8601_FORMAT:
            return V.TemporalReferential.ISO8601.value
        return V.TemporalReferential.LOCAL_STRING.value
    warnings.append(
        "time maps a value with no epoch: or format:; it is assumed to already be Unix "
        "microseconds (temporal_referential = unix_epoch_us) — declare epoch: "
        "(unix_s|unix_ms|unix_us|unix_ns|cocoa|webkit) or format: to state the source's "
        "encoding, or temporal_referential explicitly if it really is unix_epoch_us")
    return V.TemporalReferential.UNIX_EPOCH_US.value


_ANGLE_PART_NAMES = ("latitude", "longitude")


def _spatial_referential(
    holder: dict[str, Any], parts: list[str], warnings: list[str]) -> str:
    """The spatial referential: the explicit ``spatial_referential`` mapping when set,
    else ``unknown`` — never derived from a part name.

    WHY no part-name crutch: the datum is an L2 interpretation, not an L1 fact. The
    ``latitude`` / ``longitude`` parts are datum-agnostic (the source's words), so the
    referential must come from the preset stating it explicitly (``spatial_referential:
    const(epsg:4326)``); inferring ``epsg:4326`` from the presence of an angle part would
    let the L1 part name double-state — and silently decide — the CRS. When a preset maps
    coordinates but names no referential the expression resolves to ``unknown`` and cannot
    be integrated, so that mistake is SURFACED as a warning rather than left silently
    coordinate-less."""
    explicit = _norm(holder.get(SPATIAL_REFERENTIAL_NAME))
    if explicit is not None:
        return str(explicit)
    if any(part in _ANGLE_PART_NAMES for part in parts):
        warnings.append(
            f"spatial components carry {[p for p in parts if p in _ANGLE_PART_NAMES]} but "
            f"the preset states no spatial_referential; the coordinates resolve to "
            f"'{V.SpatialReferential.UNKNOWN.value}' and cannot be integrated — map "
            f"spatial_referential (e.g. const(epsg:4326)) so they are adopted at L2")
    return V.SpatialReferential.UNKNOWN.value


def assemble_source(
    records: Sequence[RawRecord],
    preset: PresetSpec,
    store: RunStore,
    *,
    source_file_id: str,
    fingerprint_sha256: str | None,
    source_file_name: str | None = None,
    source_file_path: str | None = None,
    selector: InputSelector | None = None,
    entity: str | None = None,
    concerns: str | None = None,
) -> AssembleReport:
    """Assemble one already-L0-written source's records under ONE matched preset.

    The caller (the L0 writer / future pipeline) has already written the ``source`` and
    its ``raw_record`` rows plus the ``preset`` row; this function derives
    ``raw_record_id`` / ``preset_id`` from the same natural keys (``core.ids``) and
    writes only Layer 1, in parent-before-child order (entity + expressions +
    components → the observation referencing them). ``PRAGMA foreign_keys = ON`` makes a
    missing parent raise immediately, so the layering is enforced, not assumed.

    ``entity`` / ``concerns`` are the run-level operator params (they OVERRIDE the
    preset mapping and set the ``*_origin`` columns to ``operator_param``); either
    may itself be a call-form reference (``field(...)``) resolved per row.
    """
    report = AssembleReport()

    # Seed the resolver from the UNION of field names across ALL records, not record[0]
    # alone. WHY: a heterogeneous JSON/JSONL source can introduce a column only in a LATER
    # record; seeding from the first record would resolve that column to None and silently
    # drop its values for the whole source (a forensic tool must never drop an input). HOW:
    # first-seen order across the ordered records is deterministic, so the resolver — and
    # every glob it may expand — stays reproducible run to run.
    source_fields: list[str] = []
    seen_fields: set[str] = set()
    for record in records:
        for name in record.fields:
            if name not in seen_fields:
                seen_fields.add(name)
                source_fields.append(name)
    resolve = make_resolver(source_fields)
    ctx = PipeContext(lookup_tables=preset.lookup_tables, patterns=preset.patterns)

    # Surface a preset that maps a recovery field on a PRIMARY source ONCE per source, not
    # once per record: on a 40k-row artefact the per-record form would bury every other
    # warning. _recovery_for ignores the mapping; this is what tells the operator it did.
    if preset.source_tier == V.SourceTier.PRIMARY.value:
        overruled = sorted(
            {field.model_field
             for template in preset.observations for field in template.fields
             if field.model_field in (_RECOVERY_STATE_NAME, _RECOVERY_SOURCE_NAME)})
        if overruled:
            report.warnings.append(
                f"preset {preset.meta.id} maps {', '.join(overruled)} on a PRIMARY source; "
                f"ignored — on a primary acquisition the recovery state is OUR parsing "
                f"result, not a value a preset may state")
    preset_values = _preset_values(preset, selector)
    preset_key = preset.meta.id
    # The preset row's id — derived, never invented: uuid5(key ⟂ version), the same
    # derivation the store's write_preset caller used, so the FK matches by construction.
    preset_row_id = ids.preset_id(preset_key, preset.meta.version)

    # Backs the ``filename(name|stem|path)`` refs, mirroring the old engine's holder.
    file_values: dict[str, Any] = {
        "name": source_file_name,
        "stem": Path(source_file_name).stem if source_file_name else None,
        "path": source_file_path,
    }

    base_env = _ParamEnv(entity=entity, concerns=concerns)
    entity_expr = parse_param_expr(entity)
    concerns_expr = parse_param_expr(concerns)

    # concerns_origin: who specified the value. (observation.entity_origin was dropped by
    # decision 53; entity.concerns_origin still records it for the concerns value.)
    operator_concerns = concerns is not None

    # Which template fields feed the two resolution modes (classified once, not per row).
    common_scalar, common_verbatim = _split_specs(preset.common)

    # Mapped source UIDs seen in THIS source: uid -> the line that first claimed it. Only
    # populated when the preset maps one (the generated UID cannot collide by construction).
    seen_record_uids: dict[str, int] = {}

    for record in records:
        row = record.fields
        # Carried on the record by the caller (the pipeline reads it back with the row).
        # It can no longer be re-derived: evidence-chain ids are random (decision 50).
        raw_record_id = record.raw_record_id
        assert raw_record_id is not None, (
            f"record {record.line_no} was not read back from the store: assemble writes L1 "
            f"against EXISTING raw_record rows and cannot mint their ids")

        # Per-row operator params: a call-form --entity/--concerns resolves against
        # THIS record; a plain literal is used verbatim for every row.
        row_env = base_env
        if entity_expr is not None or concerns_expr is not None:
            row_env = replace(
                base_env,
                entity=_resolve_param(entity_expr, entity, row, resolve, file_values, base_env, preset_values),
                concerns=_resolve_param(
                    concerns_expr, concerns, row, resolve, file_values, base_env, preset_values),
            )

        # The source record's own uid, resolved VERBATIM (a source key is L1 evidence: no
        # pipe, no unit, no cast) once per record and shared by every observation below.
        source_record_uid: str | None = None
        if preset.source_record_uid is not None:
            uid_value, _ = _resolve_verbatim(
                preset.source_record_uid, row, resolve, file_values, row_env, ctx, preset_values)
            source_record_uid = _text(uid_value)
            if source_record_uid is not None:
                # Invariant: a MAPPED uid must identify exactly one source record. Two records
                # claiming one uid would silently merge distinct evidence downstream (the uid
                # is what groups a record's output rows), so it raises rather than warns.
                clash = seen_record_uids.get(source_record_uid)
                if clash is not None:
                    raise ValueError(
                        f"preset {preset.meta.id!r} maps source_record_uid "
                        f"{source_record_uid!r} on both line {clash} and line "
                        f"{record.line_no} of {source_file_name or source_file_id}: a mapped uid "
                        f"must be unique per source record — map a genuinely unique column, "
                        f"or write 'source_record_uid: default' to generate one")
                seen_record_uids[source_record_uid] = record.line_no

        shared: dict[str, Any] = {}
        # Spatial parts mapped under ``common:`` become COMPONENTS shared by every observation
        # of this record (a ``common:`` coordinate is the record's base place) — resolved
        # once here, not per template.
        # (part, source_field, raw_value, declared unit)
        shared_components: list[tuple[str, str | None, str | None, str | None]] = []
        for spec in common_scalar:
            shared[spec.model_field], _ = _resolve_field(
                spec, row, report.warnings, resolve, file_values, row_env, ctx, preset_values)
        for spec in common_verbatim:
            value, source_col = _resolve_verbatim(
                spec, row, resolve, file_values, row_env, ctx, preset_values)
            if spec.model_field in V.STANDARD_SPATIAL_PARTS:
                # A ``common:``-mapped spatial part is a real corner, not a scalar: emit it as
                # a component (mirroring the per-template loop) so a shared coordinate yields a
                # spatial_expression rather than vanishing. An absent value is NO row —
                # nothing invented.
                if not _is_missing(value):
                    shared_components.append(
                        (spec.model_field, source_col, _text(value), spec.unit))
            else:
                shared[spec.model_field] = value
        # Run-level entity / concerns OVERRIDE the preset value whenever the operator
        # supplied one — INCLUDING an empty string or a call-form that resolves to nothing.
        # WHY apply on ``entity is not None`` (the operator param) rather than truthiness:
        # concerns_origin is tagged OPERATOR_PARAM on exactly that test, so
        # the APPLIED value must key on the same condition — otherwise ``--entity ""`` tags a
        # preset value it never actually replaced (origin lies about provenance).
        if entity is not None:
            shared[_ENTITY_NAME] = row_env.entity
        if concerns is not None:
            shared[_CONCERNS_NAME] = row_env.concerns

        # data_source at SOURCE grain: resolved once, from the first record
        # that yields one, and only REPORTED — the source row is the L0 writer's to set.
        if report.data_source is None:
            # The NA sentinel (a mapping that evaluated to no-match) is NOT a real path, so
            # it must leave the source row NULL rather than stamp literal "N/A". Treat it as
            # missing here — both for the primary key and the legacy fallback.
            data_source = shared.get(DATA_SOURCE_NAME)
            if _is_missing(data_source) or data_source is NA_SENTINEL:
                data_source = shared.get(RAW_SOURCE_PATH_NAME)
            if not _is_missing(data_source) and data_source is not NA_SENTINEL:
                report.data_source = str(data_source)

        # Activity is NOT written (decision 51): the table stays empty and
        # observation.activity_id stays NULL until activity is modelled.

        # --- the fan-out: one observation per template × time spec ------------
        for template in preset.observations:
            scalar_specs, verbatim_specs = _split_specs(template.fields)
            holder = dict(shared)
            for spec in scalar_specs:
                holder[spec.model_field], _ = _resolve_field(
                    spec, row, report.warnings, resolve, file_values, row_env, ctx, preset_values)
            # Spatial component candidates: (part, source_field, raw_value) in ordinal order
            # (it feeds the digest) — the record's shared ``common:`` corners FIRST, then
            # this template's own declaration order after them.
            components: list[tuple[str, str | None, str | None, str | None]] = list(
                shared_components)
            for spec in verbatim_specs:
                value, source_col = _resolve_verbatim(
                    spec, row, resolve, file_values, row_env, ctx, preset_values)
                if spec.model_field in V.STANDARD_SPATIAL_PARTS:
                    if not _is_missing(value):   # an absent source value is NO row — nothing invented
                        components.append(
                            (spec.model_field, source_col, _text(value), spec.unit))
                else:
                    holder[spec.model_field] = value

            for time_spec in template.temporal:
                flat = dict(holder)
                for override in time_spec.overrides:
                    flat[override.model_field], _ = _resolve_field(
                        override, row, report.warnings, resolve, file_values, row_env, ctx, preset_values)
                _emit_observation(
                    store, report, flat, components, time_spec,
                    row=row, resolve=resolve, file_values=file_values, env=row_env,
                    ctx=ctx, preset_values=preset_values,
                    raw_record_id=raw_record_id, preset_key=preset_key,
                    preset_row_id=preset_row_id,
                    fingerprint_sha256=fingerprint_sha256, line_no=record.line_no,
                    operator_concerns=operator_concerns,
                    source_record_uid=source_record_uid,
                    recovery=_recovery_for(preset, record, flat),
                )

    return report


def _split_specs(specs: Sequence[FieldSpec]) -> tuple[list[FieldSpec], list[FieldSpec]]:
    """Partition mapped fields into (scalar, verbatim-resolved).

    Verbatim resolution (no pipe/unit/cast — the L1 no-conversion invariant) applies to
    the spatial part fields (they become component raw_values), the two entity text fields
    (``raw_identifier`` / ``concerns`` are "as written in the source"), and the per-bound
    time-accuracy fields (their raw µs text is L1 evidence, parsed at L2 — matching the
    spatial accuracy corner and ``integrate.temporal._stated_accuracy``'s verbatim-at-L1
    contract; a preset that attaches a pipe/unit to the field must NOT convert it here).
    Everything else — links, roles, kinds, context, deleted, labels — resolves through
    the full old tail, where a lookup pipe maps tokens rather than converting values."""
    verbatim_names = {_ENTITY_NAME, _CONCERNS_NAME,
                      TIME_ACCURACY_US_LOWER_NAME, TIME_ACCURACY_US_UPPER_NAME}
    scalar: list[FieldSpec] = []
    verbatim: list[FieldSpec] = []
    for spec in specs:
        if spec.model_field in V.STANDARD_SPATIAL_PARTS or spec.model_field in verbatim_names:
            verbatim.append(spec)
        else:
            scalar.append(spec)
    return scalar, verbatim


def _write_temporal_component(
    store: RunStore,
    temporal_expression_id: str,
    ordinal: int,
    part: str,
    stated_offset: str | None,
    source_field: str | None,
    raw_value: str | None,
) -> ids.TemporalComponentProjection:
    """Write one temporal component row and return its matching digest projection.

    HOW: mints the component id from ``(temporal_expression_id, ordinal)``, writes the L2 row,
    and returns the ``TemporalComponentProjection`` the caller appends — in the SAME ordinal
    order — to the expression's projection.
    WHY a shared helper: the bound loop and the per-bound accuracy loop write the identical row
    shape (only the offset/source_field set differs), so routing both through one helper
    keeps the written row and its projection in lockstep — any drift between them would move the
    spatiotemporal_entity_hash.
    """
    store.write_temporal_component(V.TemporalComponent(
        temporal_component_id=ids.new_id(),
        temporal_expression_id=temporal_expression_id,
        ordinal=ordinal,
        part=part,
        stated_offset=stated_offset,
        source_field=source_field,
        raw_value=raw_value,
    ))
    return ids.TemporalComponentProjection(part, stated_offset, raw_value)


def _recovery_for(preset: PresetSpec, record: RawRecord,
                  flat: dict[str, Any]) -> tuple[Any, Any]:
    """The (recovery_state, recovery_source) this observation records — TIER-GATED.

    - **primary**: OUR OWN parsing. The reader found it (the sqlite reader's WAL / journal /
      carve pass), so the record carries it and a preset may not overrule it. A preset that
      maps either field on a primary source is ignored, and says so in the report — the same
      discipline as ``reported_data_source`` (decision 47c): a tool's claim never overwrites
      a fact we established ourselves.
    - **secondary**: what the TOOL REPORTS, i.e. whatever the preset mapped. The reader's own
      value is NOT used as a fallback: for a secondary export it is the format reader's
      ``LIVE`` default (a CSV or XML reader cannot know), so falling back would silently
      assert "live" about rows the tool never described. Unmapped stays NULL — the honest
      "the export does not say".

    WHY gate rather than "preset wins, else the reader": the two tiers are answering
    different questions with the same words, and a fallback would let them blend in one cell
    without the reader being able to tell which they got.
    """
    mapped_state = _norm(flat.get(_RECOVERY_STATE_NAME))
    mapped_source = _norm(flat.get(_RECOVERY_SOURCE_NAME))
    if preset.source_tier != V.SourceTier.PRIMARY.value:
        return mapped_state, mapped_source
    return record.recovery_state, record.recovery_source


def _bearer_for(
    store: RunStore, flat: dict[str, Any], entity_link: str | None, entity_id: str,
) -> tuple[str | None, ids.EntityProjection | None]:
    """The observation's ``bearer_entity_id`` and its digest projection, from the entity link.

    Decision 55, one branch per member:
    - ``subject`` — the targeted entity holds its own position: the bearer IS ``entity_id``
      and nothing is projected (the entity block already carries it).
    - ``party`` — another entity holds it: a SECOND entity row is minted even when the
      preset names no identifier — "the antenna" with no id is a purely typed mention,
      exactly like an unidentified targeted entity.
    - ``mentioned`` — a second row only when the record names the holder.
    - ``unknown`` / no link — NULL. Minting a placeholder here would manufacture a presence
      claim the source never made (the Waze case).
    ``concerns`` is not carried to the bearer: it attributes the exhibit to a party, which is
    about the targeted entity.
    """
    if entity_link == V.EntityLink.SUBJECT.value:
        return entity_id, None
    bearer_identifier = _text(flat.get(_BEARER_ENTITY_NAME))
    mints = (entity_link == V.EntityLink.PARTY.value
             or (entity_link == V.EntityLink.MENTIONED.value and bearer_identifier is not None))
    if not mints:
        return None, None
    bearer_identifier_type = (_norm(flat.get(_BEARER_IDENTIFIER_TYPE_NAME))
                              or V.IdentifierType.NONE.value)
    bearer_entity_type = (_norm(flat.get(_BEARER_ENTITY_TYPE_NAME))
                          or V.EntityType.UNKNOWN.value)
    bearer_entity_id = ids.new_id()
    store.write_entity(V.Entity(
        entity_id=bearer_entity_id,
        identifier=None,
        raw_identifier=bearer_identifier,
        identifier_type=str(bearer_identifier_type),
        type=str(bearer_entity_type),
        concerns=None,
        concerns_origin=None,
        concerns_uid=None,
    ))
    return bearer_entity_id, ids.EntityProjection(
        None, str(bearer_identifier_type), str(bearer_entity_type))


def _emit_observation(
    store: RunStore,
    report: AssembleReport,
    flat: dict[str, Any],
    components: list[tuple[str, str | None, str | None, str | None]],
    time_spec: TimeSpec,
    *,
    row: dict[str, Any],
    resolve: Callable[[str | None], str | None],
    file_values: dict[str, Any],
    env: _ParamEnv,
    ctx: PipeContext,
    preset_values: dict[str, Any],
    raw_record_id: str,
    preset_key: str,
    preset_row_id: str,
    fingerprint_sha256: str | None,
    line_no: int,
    operator_concerns: bool,
    source_record_uid: str | None,
    recovery: tuple[Any, Any],
) -> None:
    """Write one observation and its owned corners, then the observation row itself
    (parent-before-child order).

    HOW: every id written here is a fresh uuid4 (``ids.new_id``, decision 50), so two
    presets reading one record — and two templates of one preset — can never collide on a
    corner PK, without needing a discriminator in the key. Both digests come from
    ``core.ids`` over the exact rows written below — never a local recipe.
    """

    # --- entity (identifier + concerns_uid left NULL for the entity normaliser) ---
    raw_identifier = _text(flat.get(_ENTITY_NAME))
    identifier_type = _norm(flat.get(IDENTIFIER_TYPE_NAME)) or V.IdentifierType.NONE.value
    entity_type = _norm(flat.get(_ENTITY_TYPE_NAME)) or V.EntityType.UNKNOWN.value
    concerns = _text(flat.get(_CONCERNS_NAME))
    if concerns is None:
        concerns_origin = None
    else:
        concerns_origin = V.Origin.OPERATOR_PARAM if operator_concerns else V.Origin.PRESET_MAPPED
    entity_id = ids.new_id()
    store.write_entity(V.Entity(
        entity_id=entity_id,
        identifier=None,                 # the entity normaliser fills identifier +
        raw_identifier=raw_identifier,   # refines identifier_type + derives concerns_uid.
        identifier_type=str(identifier_type),
        type=str(entity_type),
        concerns=concerns,
        concerns_origin=concerns_origin,
        concerns_uid=None,
    ))

    # --- the position bearer (decision 55) ---
    entity_link = _norm(flat.get(_ENTITY_LINK_NAME))
    bearer_entity_id, bearer_projection = _bearer_for(store, flat, entity_link, entity_id)

    # --- spatial expression + components ---
    # No component values → no spatial corner: an observation without a place yields empty
    # coordinates by construction, never an invented header row.
    spatial_expression_id: str | None = None
    spatial_projection: ids.SpatialProjection | None = None
    if components:
        spatial_source = _text(flat.get(_SPATIAL_SOURCE_NAME))
        referential = _spatial_referential(
            flat, [part for part, _, _, _ in components], report.warnings)
        spatial_expression_id = ids.new_id()
        # The type the preset STATED — never derived. A preset that maps a place but names
        # no type leaves it UNKNOWN and warns: no mechanism serves `unknown`, so the place
        # cannot reach WGS84, and that must be surfaced rather than guessed at.
        position_type = _norm(flat.get(POSITION_TYPE_NAME))
        if position_type is None:
            position_type = V.PositionType.UNKNOWN.value
            report.warnings.append(
                f"spatial components are mapped but the preset states no "
                f"{POSITION_TYPE_NAME}; the place resolves to "
                f"'{V.PositionType.UNKNOWN.value}' and no method can integrate it — map "
                f"{POSITION_TYPE_NAME} (e.g. const(coordinate))")
        # The referential belongs to the two self-contained-transformation types ONLY: a
        # CRS for a coordinate, a grid system for a grid reference. A lookup type reaches
        # WGS84 through external data and has nothing to be expressed in, so its
        # referential is dropped to NULL rather than kept as a value that would read as a
        # real claim about a datum it does not have.
        if not V.PositionType(position_type).takes_referential:
            referential = None
        store.write_spatial_expression(V.SpatialExpression(
            spatial_expression_id=spatial_expression_id,
            referential=referential,
            position_type=position_type,
            spatial_source=spatial_source,
        ))
        component_projections: list[ids.SpatialComponentProjection] = []
        for ordinal, (part, source_field, raw_value, unit) in enumerate(components):
            store.write_spatial_component(V.SpatialComponent(
                spatial_component_id=ids.new_id(),
                spatial_expression_id=spatial_expression_id,
                ordinal=ordinal,
                part=part,
                source_field=source_field,
                raw_value=raw_value,
                unit=unit,
            ))
            component_projections.append(
                ids.SpatialComponentProjection(part, unit, raw_value))
        spatial_projection = ids.SpatialProjection(
            referential, spatial_source, tuple(component_projections))

    # --- temporal expression + components ---
    stated_offset: str | None = None
    if time_spec.zone is not None:
        # The stated offset is the source's OWN zone text, VERBATIM per bound. The
        # old language declares one zone per time spec, so the same text states on every
        # bound of this expression.
        zone_value, _ = _resolve_verbatim(
            time_spec.zone, row, resolve, file_values, env, ctx, preset_values)
        stated_offset = _text(zone_value)

    lower_value, lower_col = _resolve_ref(time_spec.lower, row, resolve, file_values, env, preset_values)
    lower_value = _apply_extract(lower_value, time_spec.lower_extract, ctx)
    bounds: list[tuple[str, str | None, str | None]] = []
    # Track which SIDES actually produced a bound, so each accuracy component can be gated on
    # ITS OWN bound existing (below) rather than on the total bound count — a malformed
    # interval missing one bound must not orphan or drop the other side's accuracy.
    have_lower = not _is_missing(lower_value)
    have_upper = False
    if have_lower:
        part = _text(flat.get(TEMPORAL_PART_LOWER_NAME)) or "lower"
        bounds.append((part, lower_col, _text(lower_value)))
    if time_spec.kind == "interval":
        upper_value, upper_col = _resolve_ref(time_spec.upper, row, resolve, file_values, env, preset_values)
        upper_value = _apply_extract(upper_value, time_spec.upper_extract, ctx)
        if not _is_missing(upper_value):
            have_upper = True
            # A ``duration_sec:`` bound defaults to the part ``duration`` rather than ``upper``
            # — the value is an elapsed span, not an end clock, and the part name is what tells
            # L2 (and the exporter, and a reader of the row) which of the two it is holding.
            # An explicit ``temporal_part_upper`` still wins: the preset gets the last word.
            default_part = _DURATION_PART_NAME if time_spec.duration_unit else "upper"
            part = _text(flat.get(TEMPORAL_PART_UPPER_NAME)) or default_part
            bounds.append((part, upper_col, _text(upper_value)))

    temporal_expression_id: str | None = None
    temporal_projection: ids.TemporalProjection | None = None
    if bounds:
        kind = time_spec.kind    # "instant" | "interval" — the TemporalKind tokens
        referential = _text(flat.get(TEMPORAL_REFERENTIAL_NAME)) or _temporal_referential(time_spec, report.warnings)
        temporal_source = _text(flat.get(_TEMPORAL_SOURCE_NAME))
        temporal_expression_id = ids.new_id()
        store.write_temporal_expression(V.TemporalExpression(
            temporal_expression_id=temporal_expression_id,
            kind=kind,
            referential=referential,
            temporal_source=temporal_source,
        ))
        bound_projections: list[ids.TemporalComponentProjection] = []
        ordinal = 0
        for (part, source_field, raw_value) in bounds:
            bound_projections.append(_write_temporal_component(
                store, temporal_expression_id, ordinal,
                part, stated_offset, source_field, raw_value))
            ordinal += 1
        # Source-stated per-bound accuracy: an OPTIONAL verbatim component per side (parallel
        # to the _date/_time split parts), carrying no offset/role/source_field — the raw µs
        # text is the evidence, parsed at L2. Only present when the preset mapped it, so an
        # accuracy-free expression gains no component and its spatiotemporal_entity_hash is unchanged.
        have_side = {_LOWER_SIDE_NAME: have_lower, _UPPER_SIDE_NAME: have_upper}
        for side, field_name in ((_LOWER_SIDE_NAME, TIME_ACCURACY_US_LOWER_NAME),
                                 (_UPPER_SIDE_NAME, TIME_ACCURACY_US_UPPER_NAME)):
            accuracy_raw = _text(flat.get(field_name))
            # Gate on THIS side's bound existing, not on len(bounds): an accuracy whose bound
            # is absent (a malformed interval, or an instant's upper) is written for neither
            # side — it is a value L2 could never read, so emitting it would only move the
            # spatiotemporal_entity_hash for no observable claim.
            if accuracy_raw is None or not have_side[side]:
                continue
            accuracy_part = side + _ACCURACY_PART_SUFFIX
            bound_projections.append(_write_temporal_component(
                store, temporal_expression_id, ordinal,
                accuracy_part, None, None, accuracy_raw))
            ordinal += 1
        temporal_projection = ids.TemporalProjection(kind, referential, tuple(bound_projections))

    # --- the observation (digests via core/ids — never a local recipe) ---
    spatial_link = _norm(flat.get(_SPATIAL_LINK_NAME))
    if spatial_expression_id is None and spatial_link is not None:
        # No geometry, so there is no position whose use could be described; the schema
        # CHECK would reject the row, so it is dropped here with a warning instead.
        report.warnings.append(
            f"spatial_link '{spatial_link}' stated on an observation with no position; "
            f"dropped (decision 55: no geometry => no spatial link)")
        spatial_link = None
    temporal_link = _norm(flat.get(_TEMPORAL_LINK_NAME))
    if temporal_link is not None and time_spec.kind == V.TemporalKind.INSTANT.value:
        # On an instant box and diamond coincide: write the ONE canonical member, so a fact
        # has exactly one encoding whatever the preset author wrote.
        temporal_link = V.TemporalLink(temporal_link).canonical_on_instant.value
    recovery_state, recovery_source = recovery

    l1 = ids.l1_projection(
        entity=ids.EntityProjection(None, str(identifier_type), str(entity_type)),
        bearer=bearer_projection,
        spatial=spatial_projection,
        temporal=temporal_projection,
    )
    observation_content_hash, spatiotemporal_entity_hash = ids.digests(
        fingerprint_sha256=fingerprint_sha256,
        preset_key=preset_key,
        line_no=line_no,
        l1=l1,
    )

    store.write_observation(V.Observation(
        observation_id=ids.new_id(),
        raw_record_id=raw_record_id,
        preset_id=preset_row_id,
        entity_id=entity_id,
        observation_content_hash=observation_content_hash,
        spatiotemporal_entity_hash=spatiotemporal_entity_hash,
        bearer_entity_id=bearer_entity_id,
        # activity_id stays NULL: the table exists but nothing writes it (decision 51).
        spatial_expression_id=spatial_expression_id,
        temporal_expression_id=temporal_expression_id,
        entity_link=entity_link,
        spatial_link=spatial_link,
        temporal_link=temporal_link,
        recovery_state=recovery_state,
        recovery_source=recovery_source,
        source_label=_text(flat.get(_SOURCE_LABEL_NAME)),
        input_record_id=_text(flat.get(_INPUT_RECORD_ID_NAME)),
        # Resolved ONCE per source record (not per template) and repeated on every observation
        # the record fans out into — that repetition IS its job: it groups those rows.
        source_record_uid=source_record_uid,
    ))
    report.observation_count += 1
