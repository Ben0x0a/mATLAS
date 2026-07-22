"""L2 temporal Methods: one temporal_expression (+components) -> one timespan_integration.

Defines:    the named temporal ``Method`` implementations of the refactor-plan §3
            protocol — the seven epoch decodes (``EpochMethod``: unix s/ms/µs, Apple
            CFAbsolute, WebKit, FILETIME, Excel serial), the tzdb-versioned
            ``TzLocaliseMethod``, the ``AssumeUtcMethod`` fallback, and
            ``DatetimeCombineMethod`` (date column ⊕ time column -> one instant) —
            plus ``TemporalExpressionBundle`` (the expression header + its component
            rows, the Method input), ``standard_methods`` (the ordered default set)
            and ``integrate_expression`` (first applicable method wins, writes ONE
            ``timespan_integration`` row through the store).
Used by:    the app-layer pipeline (``model_atlas.app.pipeline``) and
            tests/integrate/test_temporal.py.
Depends on: ``model_atlas.core`` (``ids`` — the integration id derivation,
            ``vocabularies`` — the ``TimespanIntegration`` family + the
            ``UTC_OFFSET_UNKNOWN`` sentinel, ``store`` — the layer-typed writer) and
            ``model_atlas.integrate.timeparse`` for the pure zone/ISO helpers
            (``parse_zone_token`` / ``parse_iso8601`` / ``local_naive_to_utc_us`` /
            ``zone_standard_offset_hours`` / ``datetime_to_us`` — their T60 home,
            folded verbatim from the retired ``transforms.builtin``).

This is the T30 port of the old ``transforms/assemble.py`` ``_decode_time`` /
``_apply_time`` logic into named, versioned Methods (refactor_plan.md §3, phase 3).
The old rules map 1:1:

- ``epoch`` decode                      -> one ``EpochMethod`` per referential.
- ``format`` + value-borne offset       -> the aware branch of ``TzLocaliseMethod`` /
  ``AssumeUtcMethod`` (the value's own offset is honoured — nothing assumed).
- ``format`` + fixed offset (no DST)    -> the fixed-token branch of
  ``TzLocaliseMethod`` (the stated base offset is applied verbatim, season-blind).
- ``format`` + ``[DST]``/``local``      -> the zoneinfo branch of
  ``TzLocaliseMethod`` (per-bound DST resolution, with the old base-mismatch /
  ambiguous-overlap / imaginary-gap warnings, now via logging).
- naive + no zone knowledge             -> ``AssumeUtcMethod`` (the old
  ``UTC_OFFSET_UNKNOWN`` fallback, promoted to an explicit named method).
- the L1 date⊕time component split (D15) -> ``DatetimeCombineMethod``.

The applied-offset discipline (decision 9 / D32)
------------------------------------------------
The stored ``utc_offset_hours_lower/_upper`` is the offset ACTUALLY APPLIED to reach
UTC for THAT bound — never the old flat model's display-zone override — with three
distinct states (the ``vocabularies.TimespanIntegration`` contract):

- a float:                 the zone was known and applied (per bound, DST-aware);
- ``None``:                not applicable — the source value was absolute UTC by
                           definition (an epoch), nothing was localised;
- ``UTC_OFFSET_UNKNOWN``:  a naive clock was materialised by ASSUMING UTC.

The D32 cells derive per bound from unix µs + that applied offset: a known offset
renders one offset-tagged local ISO (``2026-03-14T02:20:31.000000+01:00``); an
absolute-UTC source renders the exact UTC clock tagged ``+00:00`` (true and
self-describing — nothing was assumed, so no ``N/A``); an ASSUMED clock renders with
the loud trailing `` N/A``; a bound with no instant renders EMPTY cells (empty
string / NULLs — empty and N/A stay distinct, D32). Every row is method-stamped
``rule/version[/reference]`` (composites join with ``+``); a pass-through is a
method too. NOTE: the stored ``tz_localize`` token keeps the settled docs' spelling
(run_db_schema.md names ``tz_localize/2026a``) even though identifiers here use the
house British spelling.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore
from model_atlas.integrate.timeparse import (
    datetime_to_us,
    local_naive_to_utc_us,
    parse_iso8601,
    parse_zone_token,
    zone_standard_offset_hours,
)

_LOG = logging.getLogger(__name__)

_UNIX_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
_US_PER_HOUR = 3_600_000_000
_US_PER_DAY = 86_400_000_000

# --- the epoch reference constants (recorded in the method strings) ---------
# WHY recorded: the T30 contract is that an epoch row's derivation is auditable from
# its method string alone — the constant that anchored the decode is part of the rule.
_CFABSOLUTE_OFFSET_S = 978_307_200          # 1970-01-01 -> 2001-01-01 (Apple CFAbsolute)
_WIN1601_OFFSET_S = -11_644_473_600         # 1970-01-01 -> 1601-01-01 (WebKit / FILETIME)
_EXCEL_EPOCH_ISO = "1899-12-30"             # Excel 1900-system serial day 0 (Lotus bug kept)
_EXCEL_EPOCH_US = -25_569 * _US_PER_DAY     # 1899-12-30 is 25 569 days before 1970-01-01

# --- the bound part names (identifier-string constants) ---------------------
_LOWER_PART_NAME = "lower"
_UPPER_PART_NAME = "upper"
_DATE_SUFFIX_NAME = "_date"     # the D15 split parts: lower_date / upper_date
_TIME_SUFFIX_NAME = "_time"     # lower_time / upper_time
_INSTANT_KIND_NAME = V.TemporalKind.INSTANT.value

# The string referentials the zone methods can parse (epoches are numeric, not clocks).
_CLOCK_REFERENTIALS: frozenset[str] = frozenset({
    V.TemporalReferential.ISO8601.value,
    V.TemporalReferential.LOCAL_STRING.value,
})


def _tzdb_version() -> str:
    """The IANA tz database version the run resolves zones against.

    HOW: the ``tzdata`` wheel states its version; without it, ``zoneinfo`` reads the
    OS tz files, whose version is not introspectable — so the reference honestly says
    ``system``. WHY in the method string: a tzdb upgrade changes derived offsets, so
    the reference data that produced a row must be named on it (the L2 invariant).
    """
    try:
        import tzdata
        return str(tzdata.IANA_VERSION)
    except ImportError:
        return "system"


_TZDB_VERSION = _tzdb_version()   # fixed per process: one run resolves one tz database


# --- the Method input --------------------------------------------------------

@dataclass(frozen=True)
class TemporalExpressionBundle:
    """One temporal expression AS THE METHODS CONSUME IT: header + component rows.

    The components are the L1 verbatim rows in ordinal order (the assembler wrote
    them; the caller re-reads or keeps them). Frozen: L1 is immutable evidence.
    """

    expression: V.TemporalExpression
    components: tuple[V.TemporalComponent, ...]


@dataclass(frozen=True)
class _Bound:
    """One decoded bound: the canonical instant, the APPLIED offset, its accuracy.

    ``offset`` follows the three-state contract (float / None / UTC_OFFSET_UNKNOWN);
    ``accuracy_us`` is a SOURCE-provided timing uncertainty (µs) when the method was given
    one, else None — it is never invented from the clock's display resolution.
    """

    unix_us: int | None = None
    offset: float | str | None = None
    accuracy_us: int | None = None


_ABSENT_BOUND = _Bound()


# --- small shared helpers -----------------------------------------------------

def _value_of(token: object) -> str | None:
    """The bare stored token of an enum-or-string column value (None stays None).

    WHY: the family dataclasses type these columns as enums but the assembler and the
    tests legally pass the plain tokens — comparisons must not depend on which."""
    if token is None:
        return None
    return str(getattr(token, "value", token))


def _component(bundle: TemporalExpressionBundle, part: str) -> V.TemporalComponent | None:
    """The FIRST component carrying ``part`` (ordinal order), or None.

    The UNIQUE key is (expression, ordinal), not (expression, part), so a duplicate
    part is legal L1; taking the first in ordinal order is the deterministic choice.
    """
    for component in bundle.components:
        if component.part == part:
            return component
    return None


def _raw_text(component: V.TemporalComponent | None) -> str | None:
    """The component's verbatim raw_value as stripped text, or None when absent/blank."""
    if component is None or component.raw_value is None:
        return None
    text = str(component.raw_value).strip()
    return text or None


def _offset_tag(offset_hours: float) -> str:
    """Render a signed decimal-hours offset as the ISO ``±HH:MM`` tag (+05:30 style)."""
    sign = "+" if offset_hours >= 0 else "-"
    minutes = int(round(abs(offset_hours) * 60))
    hours, mins = divmod(minutes, 60)
    return f"{sign}{hours:02d}:{mins:02d}"


def _bound_cells(unix_us: int | None,
                 offset: float | str | None) -> tuple[str, int | None, int | None]:
    """The D32 convenience cells for ONE bound: (datetime_iso, date µs, time-of-day µs).

    HOW: shift the canonical UTC µs by the applied offset to get the local wall
    clock, render it with full microseconds, then tag it by the offset state:
    - no instant                  -> ("", None, None)  — EMPTY cells, never invented;
    - offset known (float)        -> ``…±HH:MM`` local ISO + local date + time µs;
    - offset None (absolute UTC)  -> the exact UTC clock tagged ``+00:00`` (true and
      self-describing — an epoch source assumed nothing, so no ``N/A``);
    - offset UNKNOWN (assumed)    -> the assumed-UTC clock with the loud trailing
      `` N/A``.
    WHY the `` N/A`` suffix: "value, zone unknown" must stay visibly distinct from
    "no value" (empty) — D32's empty ≠ N/A rule; confusing them hides an assumption.
    All datetime arithmetic is timedelta-from-epoch (never ``fromtimestamp``): the
    Windows CRT raises for pre-1970/far-future instants that occur in real evidence.
    """
    if unix_us is None:
        return "", None, None
    known = isinstance(offset, (int, float)) and not isinstance(offset, bool)
    applied = float(offset) if known else 0.0
    local_us = unix_us + int(round(applied * _US_PER_HOUR))
    local = _UNIX_EPOCH + dt.timedelta(microseconds=local_us)
    clock = local.strftime("%Y-%m-%dT%H:%M:%S.%f")
    if known:
        iso = clock + _offset_tag(applied)
    elif offset == V.UTC_OFFSET_UNKNOWN:
        iso = clock + " " + V.UTC_OFFSET_UNKNOWN
    else:
        # offset None with an instant: an absolute-UTC source — the UTC clock IS the
        # value, tagged +00:00 so the ISO stays self-describing and UTC-recoverable.
        iso = clock + "+00:00"
    day = dt.datetime(local.year, local.month, local.day, tzinfo=dt.timezone.utc)
    date_us = (day - _UNIX_EPOCH).days * _US_PER_DAY
    return iso, date_us, local_us - date_us


def _resolve_naive(naive: dt.datetime, stated_offset: str | None,
                   zone: str | None, raw: str) -> tuple[int, float | str]:
    """Resolve one naive wall clock to (unix µs, applied offset) — the old
    ``_decode_time`` zone ladder, ported branch for branch.

    Ladder (first match wins):
    1. stated ``[DST]``/``local`` token + a configured zone -> zoneinfo per-bound
       resolution (warns on a header/zone base mismatch and on the DST overlap /
       gap anomalies — the old warnings, now logged);
    2. stated DST token but NO zone -> warn and fall back to the stated base offset
       (or assumed UTC when the token carries none — ``local`` with no zone);
    3. stated fixed offset -> applied verbatim, season-blind (the source declared it);
    4. no usable token but a configured zone -> zoneinfo resolution (the configured
       zone IS the tz-localisation method's purpose — equivalent to the old
       ``local`` token, which presets spelt explicitly);
    5. nothing -> materialise ASSUMING UTC and flag the offset UNKNOWN, never a
       misleading 0.0.
    """
    token = parse_zone_token(stated_offset)
    if token is not None and token.dst:
        if zone:
            standard = zone_standard_offset_hours(zone)
            base = token.base_offset_hours
            if base is not None and abs(standard - base) > 1e-9:
                _LOG.warning(
                    f"time-zone header base offset {base}h does not match zone "
                    f"{zone!r} standard offset {standard}h; check the zone")
            unix_us, offset, anomaly = local_naive_to_utc_us(naive, zone)
            if anomaly == "ambiguous":
                _LOG.warning(
                    f"ambiguous local time {raw!r} in {zone} (DST fall-back overlap); "
                    f"recorded as the earlier instant")
            elif anomaly == "imaginary":
                _LOG.warning(
                    f"nonexistent local time {raw!r} in {zone} (DST spring-forward "
                    f"gap); check the zone")
            return unix_us, offset
        _LOG.warning(
            f"DST/local timestamp {raw!r} but no zone configured; using base offset "
            f"{token.base_offset_hours if token.base_offset_hours is not None else 0.0}h "
            f"— DST-period bounds may be off by an hour")
        if token.base_offset_hours is not None:
            return _apply_fixed(naive, token.base_offset_hours)
        return _assume_utc(naive)
    if token is not None and token.base_offset_hours is not None:
        return _apply_fixed(naive, token.base_offset_hours)
    if zone:
        unix_us, offset, anomaly = local_naive_to_utc_us(naive, zone)
        if anomaly is not None:
            _LOG.warning(f"DST-{anomaly} local time {raw!r} in {zone}; check the zone")
        return unix_us, offset
    return _assume_utc(naive)


def _apply_fixed(naive: dt.datetime, base_hours: float) -> tuple[int, float]:
    """Apply a stated FIXED offset to a naive clock (branch 3 of the ladder)."""
    aware = naive.replace(tzinfo=dt.timezone(dt.timedelta(hours=base_hours)))
    return datetime_to_us(aware), base_hours


def _assume_utc(naive: dt.datetime) -> tuple[int, str]:
    """Materialise a naive clock by ASSUMING UTC, flagging the offset UNKNOWN.

    WHY the sentinel and not 0.0: a real 0.0 would claim the zone was known to be
    UTC; the assumption must stay loud on the row (D32 / decision 9).
    """
    return datetime_to_us(naive.replace(tzinfo=dt.timezone.utc)), V.UTC_OFFSET_UNKNOWN


# --- the Method base ----------------------------------------------------------

class _TemporalMethodBase:
    """Shared skeleton of the temporal Methods (refactor_plan.md §3 protocol shape).

    Subclasses set ``name`` / ``version`` / ``reference`` and implement
    ``applies_to`` + ``_decode_bound``; ``integrate`` assembles the ONE
    ``TimespanIntegration`` row (id derived in ``core.ids`` — never a local recipe).
    """

    name: str = ""
    version: str = "1.0"
    reference: str | None = None
    # A method-level accuracy claim (µs); per-bound decode accuracies override it.
    time_accuracy_us: int | None = None

    @property
    def method(self) -> str:
        """The stored method string: ``rule/version[/reference]``."""
        parts = [self.name, self.version]
        if self.reference is not None:
            parts.append(self.reference)
        return "/".join(parts)

    def applies_to(self, expression: TemporalExpressionBundle) -> bool:
        raise NotImplementedError

    def _decode_bound(self, expression: TemporalExpressionBundle, side: str) -> _Bound:
        raise NotImplementedError

    def integrate(self, expression: TemporalExpressionBundle) -> V.TimespanIntegration | None:
        """Decode both bounds and build the ONE integration row (no store access).

        Returns None when NO bound decodes — nothing is invented for an expression
        this method cannot read (the caller may try the next method).
        """
        lower = self._decode_bound(expression, _LOWER_PART_NAME)
        upper = self._decode_bound(expression, _UPPER_PART_NAME)
        # An instant is one clock reading: both bounds carry it (the interval
        # degenerates to [t, t]), so a lone lower fills the upper — the worked
        # examples' shape, and what interval queries expect.
        kind = _value_of(expression.expression.kind)
        if kind == _INSTANT_KIND_NAME and upper.unix_us is None and lower.unix_us is not None:
            upper = lower
        if lower.unix_us is None and upper.unix_us is None:
            return None
        # The row's accuracy: the coarsest per-bound claim wins (an interval read
        # from a 1 s clock and a 1 day date is only day-accurate as a whole); the
        # method-level claim is the fallback.
        claims = [a for a in (lower.accuracy_us, upper.accuracy_us) if a is not None]
        accuracy = max(claims) if claims else self.time_accuracy_us
        lower_iso, lower_date, lower_time = _bound_cells(lower.unix_us, lower.offset)
        upper_iso, upper_date, upper_time = _bound_cells(upper.unix_us, upper.offset)
        expression_id = expression.expression.temporal_expression_id
        return V.TimespanIntegration(
            timespan_integration_id=ids.timespan_integration_id(expression_id, self.method),
            temporal_expression_id=expression_id,
            method=self.method,
            lower_unix_utc_us=lower.unix_us,
            upper_unix_utc_us=upper.unix_us,
            lower_datetime_iso=lower_iso,
            upper_datetime_iso=upper_iso,
            lower_date=lower_date,
            upper_date=upper_date,
            lower_time=lower_time,
            upper_time=upper_time,
            utc_offset_hours_lower=lower.offset,
            utc_offset_hours_upper=upper.offset,
            time_accuracy_us=accuracy,
        )


# --- the epoch decodes ---------------------------------------------------------

@dataclass(frozen=True)
class _EpochRule:
    """One epoch decode declaration: referential -> µs arithmetic + method identity.

    ``scale_num_us``/``scale_den`` express "µs per source unit" as a ratio so
    FILETIME's 100 ns ticks divide exactly; ``offset_us`` re-anchors the epoch;
    ``assumed_clock`` marks a referential that encodes a WALL CLOCK with no zone
    (Excel serials) — decoded by assuming UTC, sentinel-flagged, unlike the true
    absolute epochs.

    No accuracy is derived from the epoch: ``time_accuracy_us`` is a SOURCE-provided
    uncertainty (like the spatial corner's accuracy), not the clock's display resolution,
    so an epoch decode leaves it unset.
    """

    rule_name: str
    referential: str
    scale_num_us: int
    scale_den: int
    offset_us: int
    assumed_clock: bool
    reference: str | None


_EPOCH_RULES: tuple[_EpochRule, ...] = (
    _EpochRule("epoch_unix_s", V.TemporalReferential.UNIX_EPOCH_S.value,
               1_000_000, 1, 0, False, None),
    _EpochRule("epoch_unix_ms", V.TemporalReferential.UNIX_EPOCH_MS.value,
               1_000, 1, 0, False, None),
    _EpochRule("epoch_unix_us", V.TemporalReferential.UNIX_EPOCH_US.value,
               1, 1, 0, False, None),
    _EpochRule("epoch_cfabsolute", V.TemporalReferential.APPLE_CFABSOLUTE.value,
               1_000_000, 1, _CFABSOLUTE_OFFSET_S * 1_000_000, False,
               f"offset_s={_CFABSOLUTE_OFFSET_S}"),
    _EpochRule("epoch_webkit", V.TemporalReferential.WEBKIT.value,
               1, 1, _WIN1601_OFFSET_S * 1_000_000, False,
               f"offset_s={_WIN1601_OFFSET_S}"),
    _EpochRule("epoch_filetime", V.TemporalReferential.FILETIME.value,
               1, 10, _WIN1601_OFFSET_S * 1_000_000, False,
               f"offset_s={_WIN1601_OFFSET_S}"),
    _EpochRule("epoch_excel_serial", V.TemporalReferential.EXCEL_SERIAL.value,
               _US_PER_DAY, 1, _EXCEL_EPOCH_US, True,
               f"epoch={_EXCEL_EPOCH_ISO}"),
)


class EpochMethod(_TemporalMethodBase):
    """One numeric epoch decode, named after its referential.

    A true epoch value is absolute UTC by definition — the applied offset is None
    (not applicable) and the ISO renders the exact UTC clock tagged ``+00:00``. An
    ``assumed_clock`` referential (Excel serial) encodes a zone-less wall clock, so
    it is materialised by assuming UTC with the loud sentinel instead. The unix_us
    rule is deliberately a method too (``epoch_unix_us/1.0`` — a pass-through, but a
    documented one: derived value => documented derivation, no exceptions).
    """

    def __init__(self, rule: _EpochRule) -> None:
        self.name = rule.rule_name
        self.reference = rule.reference
        self._rule = rule

    def applies_to(self, expression: TemporalExpressionBundle) -> bool:
        return _value_of(expression.expression.referential) == self._rule.referential

    def _decode_bound(self, expression: TemporalExpressionBundle, side: str) -> _Bound:
        text = _raw_text(_component(expression, side))
        if text is None:
            return _ABSENT_BOUND
        rule = self._rule
        try:
            # Exact integer arithmetic whenever the source text is integral — float
            # µs loses sub-µs exactness for large counts (WebKit µs since 1601
            # exceed 2**53); floats are the fallback for fractional values only.
            number = int(text)
            scaled = number * rule.scale_num_us
            if rule.scale_den != 1:   # FILETIME 100 ns ticks: round half away from zero
                half = rule.scale_den // 2
                scaled = (scaled + half) // rule.scale_den if scaled >= 0 \
                    else -((-scaled + half) // rule.scale_den)
            unix_us = scaled + rule.offset_us
        except ValueError:
            try:
                value = float(text)
            except ValueError:
                _LOG.warning(f"unreadable {rule.referential} value {text!r}; bound skipped")
                return _ABSENT_BOUND
            unix_us = int(round(value * rule.scale_num_us / rule.scale_den)) + rule.offset_us
        offset = V.UTC_OFFSET_UNKNOWN if rule.assumed_clock else None
        # No accuracy from the decode: the epoch's resolution is NOT an accuracy claim.
        return _Bound(unix_us, offset)


def epoch_methods() -> tuple[EpochMethod, ...]:
    """One EpochMethod per declared rule, in declaration order."""
    return tuple(EpochMethod(rule) for rule in _EPOCH_RULES)


# --- the clock (string) methods -------------------------------------------------

class _ClockMethodBase(_TemporalMethodBase):
    """Shared parsing for the string-clock methods (ISO-8601 or strptime format)."""

    def __init__(self, datetime_format: str | None = None,
                 time_accuracy_us: int | None = None) -> None:
        self._format = datetime_format
        self.time_accuracy_us = time_accuracy_us

    def _parse(self, text: str) -> dt.datetime | None:
        """Parse the verbatim text to a datetime (aware when it carries an offset).

        A configured strptime format wins (the local_string referential — the caller
        knows the source's spelling); otherwise ISO-8601. Unparseable -> None + a
        warning, never a raise: one bad cell must not sink the expression.
        """
        if self._format is not None:
            try:
                return dt.datetime.strptime(text, self._format)
            except ValueError:
                _LOG.warning(f"value {text!r} does not match format {self._format!r}; "
                             f"bound skipped")
                return None
        parsed = parse_iso8601(text)
        if parsed is None:
            _LOG.warning(f"unparseable ISO-8601 value {text!r}; bound skipped")
        return parsed


class TzLocaliseMethod(_ClockMethodBase):
    """tz localisation: naive local clocks resolved per bound against a zone.

    Covers the old format branches: a value carrying its OWN offset is honoured as
    parsed; a stated fixed offset is applied verbatim; a ``[DST]``/``local`` token
    (or the configured IANA zone alone) resolves per bound through zoneinfo, so an
    interval crossing a DST boundary gets DIFFERENT applied offsets on its two
    bounds. The method string carries the tz database version (reference data): a
    tzdb upgrade yields a NEW method, never a silent rewrite.
    """

    name = "tz_localize"   # the settled docs' token (run_db_schema.md: tz_localize/2026a)

    def __init__(self, zone: str | None = None, datetime_format: str | None = None,
                 time_accuracy_us: int | None = None) -> None:
        super().__init__(datetime_format, time_accuracy_us)
        self._zone = zone
        self.reference = f"tzdb={_TZDB_VERSION}"

    def applies_to(self, expression: TemporalExpressionBundle) -> bool:
        # A clock referential AND some zone knowledge — a configured zone, a
        # per-bound stated offset, or a value-borne offset. Without any of those the
        # honest method is AssumeUtc, so this one declines rather than assume.
        if _value_of(expression.expression.referential) not in _CLOCK_REFERENTIALS:
            return False
        if self._zone is not None:
            return True
        for side in (_LOWER_PART_NAME, _UPPER_PART_NAME):
            component = _component(expression, side)
            if component is None:
                continue
            if parse_zone_token(component.stated_offset) is not None:
                return True
            text = _raw_text(component)
            if text is not None and self._format is None:
                parsed = parse_iso8601(text)
                if parsed is not None and parsed.tzinfo is not None:
                    return True
        return False

    def _decode_bound(self, expression: TemporalExpressionBundle, side: str) -> _Bound:
        component = _component(expression, side)
        text = _raw_text(component)
        if text is None:
            return _ABSENT_BOUND
        parsed = self._parse(text)
        if parsed is None:
            return _ABSENT_BOUND
        if parsed.tzinfo is not None:
            # The value carries its own offset — honoured as parsed (old behaviour).
            return _Bound(datetime_to_us(parsed),
                          parsed.utcoffset().total_seconds() / 3600,
                          self.time_accuracy_us)
        unix_us, offset = _resolve_naive(parsed, component.stated_offset, self._zone, text)
        return _Bound(unix_us, offset, self.time_accuracy_us)


class AssumeUtcMethod(_ClockMethodBase):
    """The explicit assumed-UTC fallback: a naive clock with NO zone knowledge.

    The instant is materialised by assuming UTC; the applied offset is the loud
    ``UTC_OFFSET_UNKNOWN`` sentinel and the ISO carries the trailing `` N/A`` — an
    assumption stamped as such, never a silent 0.0. A value that turns out to carry
    its OWN offset is honoured (nothing assumed for that bound).
    """

    name = "assume_utc"

    def applies_to(self, expression: TemporalExpressionBundle) -> bool:
        return _value_of(expression.expression.referential) in _CLOCK_REFERENTIALS

    def _decode_bound(self, expression: TemporalExpressionBundle, side: str) -> _Bound:
        text = _raw_text(_component(expression, side))
        if text is None:
            return _ABSENT_BOUND
        parsed = self._parse(text)
        if parsed is None:
            return _ABSENT_BOUND
        if parsed.tzinfo is not None:
            return _Bound(datetime_to_us(parsed),
                          parsed.utcoffset().total_seconds() / 3600,
                          self.time_accuracy_us)
        unix_us, offset = _assume_utc(parsed)
        return _Bound(unix_us, offset, self.time_accuracy_us)


class DatetimeCombineMethod(_TemporalMethodBase):
    """date column ⊕ time column -> one instant per bound (the D15 split parts).

    The L1 assembler keeps a bound spanning a date column and a time column as TWO
    verbatim component rows (``lower_date`` + ``lower_time``); this method combines
    them into one naive clock, then resolves the zone through the same ladder as
    TzLocalise (each bound's ``stated_offset`` — the time component's first, else
    the date's). A date with no time column is decoded at midnight; its accuracy is
    NOT invented (day-granularity is representation, not a stated uncertainty) — it stays
    unset unless the method carries a source-provided one. A time with no date cannot be
    anchored to an instant — that bound stays absent (empty cells), never invented.

    The method string is a composite (the demo's ``+`` convention): the combine
    rule plus the zone rule it delegates to, e.g.
    ``datetime_combine/1.0+tz_localize/1.0/tzdb=2026a``.
    """

    name = "datetime_combine"

    def __init__(self, zone: str | None = None, date_format: str = "%Y-%m-%d",
                 time_format: str = "%H:%M:%S",
                 time_accuracy_us: int | None = None) -> None:
        self._zone = zone
        self._date_format = date_format
        self._time_format = time_format
        self.time_accuracy_us = time_accuracy_us

    @property
    def method(self) -> str:
        tail = (f"{TzLocaliseMethod.name}/1.0/tzdb={_TZDB_VERSION}"
                if self._zone is not None else f"{AssumeUtcMethod.name}/1.0")
        return f"{self.name}/{self.version}+{tail}"

    def applies_to(self, expression: TemporalExpressionBundle) -> bool:
        split_parts = {side + suffix
                       for side in (_LOWER_PART_NAME, _UPPER_PART_NAME)
                       for suffix in (_DATE_SUFFIX_NAME, _TIME_SUFFIX_NAME)}
        return any(component.part in split_parts for component in expression.components)

    def _decode_bound(self, expression: TemporalExpressionBundle, side: str) -> _Bound:
        date_component = _component(expression, side + _DATE_SUFFIX_NAME)
        time_component = _component(expression, side + _TIME_SUFFIX_NAME)
        date_text = _raw_text(date_component)
        time_text = _raw_text(time_component)
        if date_text is None:
            if time_text is not None:
                # Invariant: a time-of-day alone fixes no instant — decoding it would
                # invent a date, so the bound stays absent and the gap is logged.
                _LOG.warning(f"time-of-day {time_text!r} has no date column to anchor "
                             f"it; bound skipped")
            return _ABSENT_BOUND
        try:
            if time_text is not None:
                naive = dt.datetime.strptime(f"{date_text} {time_text}",
                                             f"{self._date_format} {self._time_format}")
            else:
                naive = dt.datetime.strptime(date_text, self._date_format)
            # Accuracy is a source-provided uncertainty, never derived from the date's
            # day-granularity (that is representation resolution, not an accuracy claim).
            accuracy = self.time_accuracy_us
        except ValueError:
            _LOG.warning(f"date/time pair ({date_text!r}, {time_text!r}) does not match "
                         f"({self._date_format!r}, {self._time_format!r}); bound skipped")
            return _ABSENT_BOUND
        # The bound's stated offset: the time component's when present (the clock
        # column is where sources state zones), falling back to the date's.
        stated = None
        if time_component is not None and time_component.stated_offset is not None:
            stated = time_component.stated_offset
        elif date_component is not None:
            stated = date_component.stated_offset
        raw = date_text if time_text is None else f"{date_text} {time_text}"
        unix_us, offset = _resolve_naive(naive, stated, self._zone, raw)
        return _Bound(unix_us, offset, accuracy)


# --- the default set + the store-writing entry point ----------------------------

def standard_methods(zone: str | None = None, datetime_format: str | None = None,
                     date_format: str = "%Y-%m-%d", time_format: str = "%H:%M:%S",
                     ) -> tuple[_TemporalMethodBase, ...]:
    """The ordered default method set for one source's configuration.

    Order matters (first applicable wins): the epoch decodes are referential-gated
    and disjoint; the combine method claims only split-part expressions; TzLocalise
    claims clock expressions WITH zone knowledge; AssumeUtc is the deliberate last
    resort so an assumption is only ever made when nothing better applies.
    """
    return (
        *epoch_methods(),
        DatetimeCombineMethod(zone=zone, date_format=date_format, time_format=time_format),
        TzLocaliseMethod(zone=zone, datetime_format=datetime_format),
        AssumeUtcMethod(datetime_format=datetime_format),
    )


def integrate_expression(store: RunStore, expression: TemporalExpressionBundle,
                         methods: tuple[_TemporalMethodBase, ...] | list[_TemporalMethodBase],
                         ) -> V.TimespanIntegration | None:
    """Integrate ONE temporal expression: first applicable method wins, one row written.

    HOW: walk the ordered methods; the first whose ``applies_to`` accepts AND whose
    ``integrate`` yields a row writes it through the layer-typed store and returns
    it. A method that applies but decodes nothing lets the NEXT method try (its
    parse failed; a later, laxer rule may still read the value honestly).
    WHY one row: the v1 schema is single-integration (UNIQUE temporal_expression_id)
    — writing a second candidate raises in the store, never merges silently.
    """
    for method in methods:
        if not method.applies_to(expression):
            continue
        row = method.integrate(expression)
        if row is not None:
            store.write_timespan_integration(row)
            return row
        _LOG.debug(f"method {method.method} applied but decoded no bound for "
                   f"expression {expression.expression.temporal_expression_id}")
    return None
