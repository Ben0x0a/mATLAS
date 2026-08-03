"""The L1 writer: records × one matched preset → assertions / expressions / components.

Defines:    ``assemble_source`` (the fan-out engine: one reader record × one matched
            preset → activity(+link), entity, spatial/temporal expressions with their
            verbatim component rows, and the assertion tying them together, written into
            a ``core.store.RunStore``), ``AssembleReport`` (its testable summary, carrying
            the source-grain ``data_source`` for the caller), and the
            EXTENDED preset field vocabulary (``entity_role``, ``identifier_type``,
            ``activity_kind``, ``context.<key>``, ``multiplicity``, the two referentials,
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
EMISSION: instead of one flat 45-column row per assertion, the engine writes the
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
- ``position_type``     → the components' ``spatial_role`` (its semantics
  always were the place's role; the old language carries ONE role per assertion, so it
  is applied to every component of that expression — one value-group per assertion).
- ``position_source``   → ``spatial_expression.spatial_source``.
- ``temporal_source``   → ``temporal_expression.temporal_source``.
- ``deleted``           → ``assertion.deleted`` (the app's OWN flag, distinct from
  ``raw_record.recovery_state``).
- ``source_label``      → ``assertion.source_label`` (the one per-claim provenance field).
- ``raw_source_path``   → the source-grain ``data_source``: assemble never
  emits it per assertion — it resolves the value once (first record) and RETURNS it on
  the report so the L0 owner can set it on the ``source`` row. ``data_source`` is the
  preferred new spelling; ``raw_source_path`` is accepted as its legacy alias.
- the three ``links``   → the assertion link columns, joined by the new ``entity_role``.
- spatial part fields (``raw_position``, ``latitude_wgs84``, … ) → one
  ``spatial_component`` row each, VERBATIM ``raw_value`` + the resolved ``source_field``.
- the time bounds       → ``temporal_component`` rows (parts ``lower``/``upper``, or a
  preset-renamed part such as ``duration``), VERBATIM, with the resolved ``zone`` text
  as each bound's ``stated_offset`` (the old language states one zone per time
  spec, so the same verbatim text is stated on every bound of that expression).

Extended vocabulary — new L1 columns the old flat model never had. These are ordinary
``FieldSpec`` targets resolved through the same machinery (so conditionals
and templates apply to them for free); the v3 YAML loader does not admit
them yet (its ``ASSIGNABLE_FIELDS`` gate lives in the fenced ``presets/spec.py``), so
until the preset schema is extended they are constructed programmatically:
``entity_role``, ``identifier_type``, ``activity_kind`` (its presence in ``common:``
creates the record's activity), ``context.<key>`` (gathered into
``activity.context_json``), ``multiplicity``, ``spatial_referential`` /
``temporal_referential`` (explicit override of the derivations below), and
``temporal_role_lower`` / ``temporal_role_upper`` / ``temporal_part_lower`` /
``temporal_part_upper`` (each bound carries its own role/part).

No conversion at L1 (the run_db_schema.md invariant)
----------------------------------------------------
Component ``raw_value``s and the entity's ``raw_identifier`` are resolved VERBATIM:
conditionals choose a source value and ``extract`` selects source text (still verbatim
characters), but the pipe / unit / cast tail is deliberately NOT applied — every
conversion is Layer-2's job, method-stamped. Scalar assertion columns
(links, roles, ``deleted``, activity kind, context values…) resolve through the FULL
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
# position_source, temporal_source, deleted, source_label, the three link columns).

ENTITY_ROLE_NAME = "entity_role"
IDENTIFIER_TYPE_NAME = "identifier_type"
ACTIVITY_KIND_NAME = "activity_kind"
CONTEXT_PREFIX_NAME = "context."           # context.<key> → activity.context_json[<key>]
MULTIPLICITY_NAME = "multiplicity"
SPATIAL_REFERENTIAL_NAME = "spatial_referential"
TEMPORAL_REFERENTIAL_NAME = "temporal_referential"
TEMPORAL_ROLE_LOWER_NAME = "temporal_role_lower"
TEMPORAL_ROLE_UPPER_NAME = "temporal_role_upper"
TEMPORAL_PART_LOWER_NAME = "temporal_part_lower"
TEMPORAL_PART_UPPER_NAME = "temporal_part_upper"
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
_SPATIAL_ROLE_NAME = "position_type"       # position_type IS the spatial role
_SPATIAL_SOURCE_NAME = "position_source"
_TEMPORAL_SOURCE_NAME = "temporal_source"
_DELETED_NAME = "deleted"
_SOURCE_LABEL_NAME = "source_label"
_INPUT_RECORD_ID_NAME = "input_record_id"
_EPL_NAME = "entity_position_link"
_ETL_NAME = "entity_time_link"
_STL_NAME = "spatial_temporal_link"

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
    resolved but does NOT write itself (``data_source`` belongs on the
    ``source`` row, which the L0 writer owns — assemble only reports it)."""

    assertion_count: int = 0
    activity_count: int = 0
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


def _temporal_referential(time_spec: TimeSpec) -> str:
    """Derive the temporal referential from the time spec's decode declaration.

    HOW: an ``epoch`` maps through ``_EPOCH_REFERENTIAL``; a ``format`` is ``iso8601``
    or a local wall-clock string; neither means the old contract "the field is already
    Unix microseconds". WHY derived here: the old preset language never named the
    referential — the decode declaration IS that information, so deriving it keeps
    existing presets expressive without a new mandatory field (an explicit
    ``temporal_referential`` mapping overrides this)."""
    if time_spec.epoch is not None:
        return _EPOCH_REFERENTIAL.get(time_spec.epoch, V.TemporalReferential.UNKNOWN.value)
    if time_spec.format is not None:
        if time_spec.format == ISO8601_FORMAT:
            return V.TemporalReferential.ISO8601.value
        return V.TemporalReferential.LOCAL_STRING.value
    return V.TemporalReferential.UNIX_EPOCH_US.value


def _spatial_referential(holder: dict[str, Any], parts: list[str]) -> str:
    """The spatial referential: the explicit ``spatial_referential`` mapping when set,
    else ``epsg:4326`` when the components carry a WGS84 latitude/longitude, else
    ``unknown`` — never invented beyond what the mapping states."""
    explicit = _norm(holder.get(SPATIAL_REFERENTIAL_NAME))
    if explicit is not None:
        return str(explicit)
    if any(part in ("latitude_wgs84", "longitude_wgs84") for part in parts):
        return V.SpatialReferential.EPSG_4326.value
    return V.SpatialReferential.UNKNOWN.value


def assemble_source(
    records: Sequence[RawRecord],
    preset: PresetSpec,
    store: RunStore,
    *,
    source_id: str,
    fingerprint_sha256: str | None,
    source_file_name: str | None = None,
    source_file_path: str | None = None,
    selector: InputSelector | None = None,
    entity: str | None = None,
    concerns: str | None = None,
    activity_ordinals: dict[str, int] | None = None,
) -> AssembleReport:
    """Assemble one already-L0-written source's records under ONE matched preset.

    The caller (the L0 writer / future pipeline) has already written the ``source`` and
    its ``raw_record`` rows plus the ``preset`` row; this function derives
    ``raw_record_id`` / ``preset_id`` from the same natural keys (``core.ids``) and
    writes only Layer 1, in parent-before-child order (activity+link → entity + expressions +
    components → the assertion referencing them). ``PRAGMA foreign_keys = ON`` makes a
    missing parent raise immediately, so the layering is enforced, not assumed.

    ``entity`` / ``concerns`` are the run-level operator params (they OVERRIDE the
    preset mapping and set the ``*_origin`` columns to ``operator_param``); either
    may itself be a call-form reference (``field(...)``) resolved per row.

    ``activity_ordinals`` is the per-run ``raw_record_id → next activity ordinal``
    registry. WHY it is caller-shared: ``activity_id`` derives from the record and an
    ordinal (id scheme — the preset is deliberately absent so the id is record-scoped),
    so when SEVERAL presets each map an activity onto one record the ordinals must
    continue across the assemble calls; the caller passes one dict for the whole run
    (preset application order is pinned by the operator's list, so the ids stay
    deterministic). Omitting it (one preset per source) uses a fresh private registry.
    """
    report = AssembleReport()
    if not records:
        return report
    if activity_ordinals is None:
        activity_ordinals = {}

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

    # entity_origin / concerns_origin: who specified the value.
    entity_origin = V.Origin.OPERATOR_PARAM if entity is not None else V.Origin.PRESET_MAPPED
    operator_concerns = concerns is not None

    # Which template fields feed the two resolution modes (classified once, not per row).
    common_scalar, common_verbatim = _split_specs(preset.common)

    for record in records:
        row = record.fields
        raw_record_id = ids.raw_record_id(source_id, record.line_no)

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

        shared: dict[str, Any] = {}
        # Spatial parts mapped under ``common:`` become COMPONENTS shared by every assertion
        # of this record (a ``common:`` coordinate is the record's base place) — resolved
        # once here, not per template.
        shared_components: list[tuple[str, str | None, str | None]] = []
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
                    shared_components.append((spec.model_field, source_col, _text(value)))
            else:
                shared[spec.model_field] = value
        # Run-level entity / concerns OVERRIDE the preset value whenever the operator
        # supplied one — INCLUDING an empty string or a call-form that resolves to nothing.
        # WHY apply on ``entity is not None`` (the operator param) rather than truthiness:
        # entity_origin / concerns_origin are tagged OPERATOR_PARAM on exactly that test, so
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

        # --- activity (+ its ONE ingest link) -------------------------------
        # Created only when the preset maps activity_kind in common: (a
        # single placement has nothing to group, so no kind mapped means no row) and the
        # kind resolved to a value. All of the record's fan-out assertions share it.
        activity_id: str | None = None
        activity_projection: ids.ActivityProjection | None = None
        kind_value = _norm(shared.get(ACTIVITY_KIND_NAME))
        if kind_value is not None:
            ordinal = activity_ordinals.get(raw_record_id, 0)
            activity_ordinals[raw_record_id] = ordinal + 1
            activity_id = ids.activity_id(raw_record_id, ordinal)
            # the preset-mapped context (callee, caller IMEI, recipients…)
            # gathered from context.<key> mappings; missing values are simply absent —
            # never invented (the sentinel, a produced value, flows as text).
            context = {
                key[len(CONTEXT_PREFIX_NAME):]: value
                for key, value in shared.items()
                if key.startswith(CONTEXT_PREFIX_NAME) and not _is_missing(value)
            }
            store.write_activity(V.Activity(
                activity_id=activity_id,
                kind=str(kind_value),
                context_json=canonical_json(context) if context else None,
            ))
            # invariant: exactly ONE activity_record link at ingest — its own record.
            store.write_activity_record(V.ActivityRecord(
                activity_record_id=ids.activity_record_id(activity_id, raw_record_id),
                activity_id=activity_id,
                raw_record_id=raw_record_id,
            ))
            report.activity_count += 1
            # The digest hashes the DECODED context object; l1_projection
            # re-canonicalises it, so key order can never move a digest.
            activity_projection = ids.ActivityProjection(str(kind_value), context or None)

        # --- the fan-out: one assertion per template × time spec ------------
        output_ordinal = 0
        for template in preset.assertions:
            scalar_specs, verbatim_specs = _split_specs(template.fields)
            holder = dict(shared)
            for spec in scalar_specs:
                holder[spec.model_field], _ = _resolve_field(
                    spec, row, report.warnings, resolve, file_values, row_env, ctx, preset_values)
            # Spatial component candidates: (part, source_field, raw_value) in ordinal order
            # (it feeds the digest) — the record's shared ``common:`` corners FIRST, then
            # this template's own declaration order after them.
            components: list[tuple[str, str | None, str | None]] = list(shared_components)
            for spec in verbatim_specs:
                value, source_col = _resolve_verbatim(
                    spec, row, resolve, file_values, row_env, ctx, preset_values)
                if spec.model_field in V.STANDARD_SPATIAL_PARTS:
                    if not _is_missing(value):   # an absent source value is NO row — nothing invented
                        components.append((spec.model_field, source_col, _text(value)))
                else:
                    holder[spec.model_field] = value

            for time_spec in template.temporal:
                flat = dict(holder)
                for override in time_spec.overrides:
                    flat[override.model_field], _ = _resolve_field(
                        override, row, report.warnings, resolve, file_values, row_env, ctx, preset_values)
                _emit_assertion(
                    store, report, flat, components, time_spec,
                    row=row, resolve=resolve, file_values=file_values, env=row_env,
                    ctx=ctx, preset_values=preset_values,
                    raw_record_id=raw_record_id, preset_key=preset_key,
                    preset_row_id=preset_row_id, output_ordinal=output_ordinal,
                    fingerprint_sha256=fingerprint_sha256, line_no=record.line_no,
                    activity_id=activity_id, activity_projection=activity_projection,
                    entity_origin=entity_origin, operator_concerns=operator_concerns,
                )
                output_ordinal += 1

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
    temporal_role: str | None,
    stated_offset: str | None,
    source_field: str | None,
    raw_value: str | None,
) -> ids.TemporalComponentProjection:
    """Write one temporal component row and return its matching digest projection.

    HOW: mints the component id from ``(temporal_expression_id, ordinal)``, writes the L2 row,
    and returns the ``TemporalComponentProjection`` the caller appends — in the SAME ordinal
    order — to the expression's projection.
    WHY a shared helper: the bound loop and the per-bound accuracy loop write the identical row
    shape (only the role/offset/source_field set differs), so routing both through one helper
    keeps the written row and its projection in lockstep — any drift between them would move the
    claim_digest.
    """
    store.write_temporal_component(V.TemporalComponent(
        temporal_component_id=ids.temporal_component_id(temporal_expression_id, ordinal),
        temporal_expression_id=temporal_expression_id,
        ordinal=ordinal,
        part=part,
        temporal_role=temporal_role,
        stated_offset=stated_offset,
        source_field=source_field,
        raw_value=raw_value,
    ))
    return ids.TemporalComponentProjection(part, temporal_role, stated_offset, raw_value)


def _emit_assertion(
    store: RunStore,
    report: AssembleReport,
    flat: dict[str, Any],
    components: list[tuple[str, str | None, str | None]],
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
    output_ordinal: int,
    fingerprint_sha256: str | None,
    line_no: int,
    activity_id: str | None,
    activity_projection: ids.ActivityProjection | None,
    entity_origin: V.Origin,
    operator_concerns: bool,
) -> None:
    """Write one assertion and its owned corners, then the assertion row itself
    (parent-before-child order).

    HOW: the corner ids all derive from the assertion natural key
    ``(raw_record_id ⟂ preset key ⟂ output_ordinal)`` — the preset key is IN the corner
    keys (unlike the worked-example demo, which had one preset per source) so two presets reading
    one record can never collide on a corner PK. Both digests come from ``core.ids``
    over the exact rows written below — never a local recipe.
    """
    natural_key = (raw_record_id, preset_key, output_ordinal)

    # --- entity (identifier + concerns_uid left NULL for the entity normaliser) ---
    raw_identifier = _text(flat.get(_ENTITY_NAME))
    identifier_type = _norm(flat.get(IDENTIFIER_TYPE_NAME)) or V.IdentifierType.NONE.value
    entity_type = _norm(flat.get(_ENTITY_TYPE_NAME)) or V.EntityType.UNKNOWN.value
    concerns = _text(flat.get(_CONCERNS_NAME))
    if concerns is None:
        concerns_origin = None
    else:
        concerns_origin = V.Origin.OPERATOR_PARAM if operator_concerns else V.Origin.PRESET_MAPPED
    entity_id = ids.entity_id(natural_key)
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

    # --- spatial expression + components ---
    # No component values → no spatial corner: an assertion without a place yields empty
    # coordinates by construction, never an invented header row.
    spatial_expression_id: str | None = None
    spatial_projection: ids.SpatialProjection | None = None
    if components:
        spatial_role = _text(flat.get(_SPATIAL_ROLE_NAME))
        spatial_source = _text(flat.get(_SPATIAL_SOURCE_NAME))
        referential = _spatial_referential(flat, [part for part, _, _ in components])
        spatial_expression_id = ids.spatial_expression_id(natural_key)
        store.write_spatial_expression(V.SpatialExpression(
            spatial_expression_id=spatial_expression_id,
            referential=referential,
            spatial_source=spatial_source,
        ))
        component_projections: list[ids.SpatialComponentProjection] = []
        for ordinal, (part, source_field, raw_value) in enumerate(components):
            store.write_spatial_component(V.SpatialComponent(
                spatial_component_id=ids.spatial_component_id(spatial_expression_id, ordinal),
                spatial_expression_id=spatial_expression_id,
                ordinal=ordinal,
                part=part,
                spatial_role=spatial_role,
                source_field=source_field,
                raw_value=raw_value,
            ))
            component_projections.append(
                ids.SpatialComponentProjection(part, spatial_role, raw_value))
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
    bounds: list[tuple[str, str | None, str | None, str | None]] = []
    # Track which SIDES actually produced a bound, so each accuracy component can be gated on
    # ITS OWN bound existing (below) rather than on the total bound count — a malformed
    # interval missing one bound must not orphan or drop the other side's accuracy.
    have_lower = not _is_missing(lower_value)
    have_upper = False
    if have_lower:
        part = _text(flat.get(TEMPORAL_PART_LOWER_NAME)) or "lower"
        bounds.append((part, _text(flat.get(TEMPORAL_ROLE_LOWER_NAME)), lower_col, _text(lower_value)))
    if time_spec.kind == "interval":
        upper_value, upper_col = _resolve_ref(time_spec.upper, row, resolve, file_values, env, preset_values)
        upper_value = _apply_extract(upper_value, time_spec.upper_extract, ctx)
        if not _is_missing(upper_value):
            have_upper = True
            part = _text(flat.get(TEMPORAL_PART_UPPER_NAME)) or "upper"
            bounds.append((part, _text(flat.get(TEMPORAL_ROLE_UPPER_NAME)), upper_col, _text(upper_value)))

    temporal_expression_id: str | None = None
    temporal_projection: ids.TemporalProjection | None = None
    if bounds:
        kind = time_spec.kind    # "instant" | "interval" — the TemporalKind tokens
        referential = _text(flat.get(TEMPORAL_REFERENTIAL_NAME)) or _temporal_referential(time_spec)
        temporal_source = _text(flat.get(_TEMPORAL_SOURCE_NAME))
        temporal_expression_id = ids.temporal_expression_id(natural_key)
        store.write_temporal_expression(V.TemporalExpression(
            temporal_expression_id=temporal_expression_id,
            kind=kind,
            referential=referential,
            temporal_source=temporal_source,
        ))
        bound_projections: list[ids.TemporalComponentProjection] = []
        ordinal = 0
        for (part, role, source_field, raw_value) in bounds:
            bound_projections.append(_write_temporal_component(
                store, temporal_expression_id, ordinal,
                part, role, stated_offset, source_field, raw_value))
            ordinal += 1
        # Source-stated per-bound accuracy: an OPTIONAL verbatim component per side (parallel
        # to the _date/_time split parts), carrying no offset/role/source_field — the raw µs
        # text is the evidence, parsed at L2. Only present when the preset mapped it, so an
        # accuracy-free expression gains no component and its claim_digest is unchanged.
        have_side = {_LOWER_SIDE_NAME: have_lower, _UPPER_SIDE_NAME: have_upper}
        for side, field_name in ((_LOWER_SIDE_NAME, TIME_ACCURACY_US_LOWER_NAME),
                                 (_UPPER_SIDE_NAME, TIME_ACCURACY_US_UPPER_NAME)):
            accuracy_raw = _text(flat.get(field_name))
            # Gate on THIS side's bound existing, not on len(bounds): an accuracy whose bound
            # is absent (a malformed interval, or an instant's upper) is written for neither
            # side — it is a value L2 could never read, so emitting it would only move the
            # claim_digest for no observable claim.
            if accuracy_raw is None or not have_side[side]:
                continue
            accuracy_part = side + _ACCURACY_PART_SUFFIX
            bound_projections.append(_write_temporal_component(
                store, temporal_expression_id, ordinal,
                accuracy_part, None, None, None, accuracy_raw))
            ordinal += 1
        temporal_projection = ids.TemporalProjection(kind, referential, tuple(bound_projections))

    # --- the assertion (links + digests via core/ids — never a local recipe) ---
    links = ids.AssertionLinks(
        entity_role=_norm(flat.get(ENTITY_ROLE_NAME)),
        entity_position_link=_norm(flat.get(_EPL_NAME)),
        entity_time_link=_norm(flat.get(_ETL_NAME)),
        spatial_temporal_link=_norm(flat.get(_STL_NAME)),
    )
    deleted = _norm(flat.get(_DELETED_NAME))
    multiplicity_value = _norm(flat.get(MULTIPLICITY_NAME))
    # NULL means 1. The sentinel (text) cannot live in an INTEGER column, so a
    # multiplicity that produced nothing is NULL — the "=1" default, nothing invented.
    multiplicity = None
    if multiplicity_value is not None and multiplicity_value is not NA_SENTINEL:
        multiplicity = int(multiplicity_value)

    l1 = ids.l1_projection(
        entity=ids.EntityProjection(None, str(identifier_type), str(entity_type)),
        spatial=spatial_projection,
        temporal=temporal_projection,
        links=links,
        deleted=deleted,
        multiplicity=multiplicity,
        activity=activity_projection,
    )
    content_hash, claim_digest = ids.digests(
        fingerprint_sha256=fingerprint_sha256,
        preset_key=preset_key,
        line_no=line_no,
        output_ordinal=output_ordinal,
        l1=l1,
    )

    store.write_assertion(V.Assertion(
        assertion_id=ids.assertion_id(raw_record_id, preset_key, output_ordinal),
        raw_record_id=raw_record_id,
        preset_id=preset_row_id,
        output_ordinal=output_ordinal,
        entity_id=entity_id,
        content_hash=content_hash,
        claim_digest=claim_digest,
        entity_origin=entity_origin,
        activity_id=activity_id,
        spatial_expression_id=spatial_expression_id,
        temporal_expression_id=temporal_expression_id,
        entity_role=links.entity_role,
        entity_position_link=links.entity_position_link,
        entity_time_link=links.entity_time_link,
        spatial_temporal_link=links.spatial_temporal_link,
        deleted=deleted,
        multiplicity=multiplicity,
        source_label=_text(flat.get(_SOURCE_LABEL_NAME)),
        input_record_id=_text(flat.get(_INPUT_RECORD_ID_NAME)),
    ))
    report.assertion_count += 1
