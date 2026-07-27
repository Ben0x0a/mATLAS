"""T30 verify suite: the temporal Methods and their timespan_integration rows.

Defines:    the tests for ``model_atlas/integrate/temporal.py`` — the task's Verify
            bullets (dual per-bound DST offsets, the assumed-UTC ``N/A`` discipline,
            empty cells for an absent bound, the epoch constants recorded in the
            method strings, every row method-stamped) plus the D32 cell shapes, the
            id derivation and the first-applicable-method dispatch. The DST cases
            reuse the old corpus (tests/transforms/test_dst.py: Paris winter +1 /
            summer +2, the AXIOM ``UTC+01:00[DST]`` token, the fixed-offset and
            captured-offset cases of test_timezone.py).
Used by:    pytest.
Depends on: ``model_atlas.integrate.temporal``, ``model_atlas.core`` (ids,
            vocabularies, store).
"""
from __future__ import annotations

import datetime as dt

import pytest

from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore
from model_atlas.integrate.temporal import (
    AssumeUtcMethod,
    DatetimeCombineMethod,
    EpochMethod,
    TemporalExpressionBundle,
    TzLocaliseMethod,
    epoch_methods,
    integrate_expression,
    standard_methods,
)

_UTC = dt.timezone.utc
_US_PER_DAY = 86_400_000_000


def _us(*args: int) -> int:
    """Unix µs of a UTC datetime given as (y, m, d, h, mi, s[, µs])."""
    moment = dt.datetime(*args, tzinfo=_UTC)
    delta = moment - dt.datetime(1970, 1, 1, tzinfo=_UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


@pytest.fixture()
def store():
    with RunStore() as s:
        yield s


def _bundle(store: RunStore, key: str, kind: str, referential: str,
            components: list[tuple[str, str | None, str | None]],
            ) -> TemporalExpressionBundle:
    """Write one temporal expression + its components and return the Method input.

    ``components`` rows are (part, stated_offset, raw_value); ids derive from
    ``core.ids`` exactly as the assembler's do, so the integration FK matches.
    """
    expression_id = ids.temporal_expression_id((f"record-{key}", key, 0))
    expression = V.TemporalExpression(
        temporal_expression_id=expression_id, kind=kind, referential=referential)
    store.write_temporal_expression(expression)
    rows = []
    for ordinal, (part, stated_offset, raw_value) in enumerate(components):
        component = V.TemporalComponent(
            temporal_component_id=ids.temporal_component_id(expression_id, ordinal),
            temporal_expression_id=expression_id,
            ordinal=ordinal,
            part=part,
            stated_offset=stated_offset,
            raw_value=raw_value,
        )
        store.write_temporal_component(component)
        rows.append(component)
    return TemporalExpressionBundle(expression=expression, components=tuple(rows))


def _row(store: RunStore, bundle: TemporalExpressionBundle) -> dict:
    """The written timespan_integration row for a bundle, as a column dict."""
    cursor = store.connection.execute(
        "SELECT * FROM timespan_integration WHERE temporal_expression_id = ?",
        (bundle.expression.temporal_expression_id,))
    names = [d[0] for d in cursor.description]
    fetched = cursor.fetchone()
    assert fetched is not None, "no integration row written"
    return dict(zip(names, fetched))


# --- Verify bullet 1: dual per-bound offsets across a DST boundary ------------

def test_dst_boundary_interval_gets_dual_per_bound_offsets(store) -> None:
    # Paris falls back 03:00 -> 02:00 on 2025-10-26: the same stated token
    # UTC+01:00[DST] resolves to +2 (summer) on the lower bound and +1 (winter) on
    # the upper — stated and applied must DIFFER, per bound (decision 9).
    method = TzLocaliseMethod(zone="Europe/Paris",
                              datetime_format="%d.%m.%Y %H:%M:%S.%f")
    bundle = _bundle(store, "dst", "interval", "local_string", [
        ("lower", "UTC+01:00[DST]", "25.10.2025 13:00:00.000"),
        ("upper", "UTC+01:00[DST]", "26.10.2025 13:00:00.000"),
    ])
    integrate_expression(store, bundle, [method])
    row = _row(store, bundle)
    assert float(row["utc_offset_hours_lower"]) == 2.0     # summer, applied
    assert float(row["utc_offset_hours_upper"]) == 1.0     # winter, applied
    # The L1 stated offset is untouched — one token, two different applied values.
    assert bundle.components[0].stated_offset == "UTC+01:00[DST]"
    assert row["lower_unix_utc_us"] == _us(2025, 10, 25, 11, 0, 0)   # 13:00 +2
    assert row["upper_unix_utc_us"] == _us(2025, 10, 26, 12, 0, 0)   # 13:00 +1
    assert row["lower_datetime_iso"] == "2025-10-25T13:00:00.000000+02:00"
    assert row["upper_datetime_iso"] == "2025-10-26T13:00:00.000000+01:00"


def test_winter_and_summer_wall_clocks_resolve_different_utc(store) -> None:
    # The old corpus case (test_dst): the same 13:00 wall clock is 12:00Z in winter
    # and 11:00Z in summer.
    method = TzLocaliseMethod(zone="Europe/Paris",
                              datetime_format="%d.%m.%Y %H:%M:%S.%f")
    winter = _bundle(store, "winter", "instant", "local_string",
                     [("lower", "UTC+01:00[DST]", "06.12.2025 13:00:00.000")])
    summer = _bundle(store, "summer", "instant", "local_string",
                     [("lower", "UTC+01:00[DST]", "06.07.2025 13:00:00.000")])
    integrate_expression(store, winter, [method])
    integrate_expression(store, summer, [method])
    assert _row(store, winter)["lower_unix_utc_us"] == _us(2025, 12, 6, 12, 0, 0)
    assert _row(store, summer)["lower_unix_utc_us"] == _us(2025, 7, 6, 11, 0, 0)


def test_stated_fixed_offset_is_applied_season_blind(store) -> None:
    # A fixed UTC+01:00 token (no [DST]) applies +1 even in July, zone configured or
    # not — the source declared the offset (old fixed-offset branch).
    method = TzLocaliseMethod(zone="Europe/Paris",
                              datetime_format="%d.%m.%Y %H:%M:%S.%f")
    bundle = _bundle(store, "fixed", "instant", "local_string",
                     [("lower", "UTC+01:00", "06.07.2025 13:00:00.000")])
    integrate_expression(store, bundle, [method])
    row = _row(store, bundle)
    assert float(row["utc_offset_hours_lower"]) == 1.0
    assert row["lower_unix_utc_us"] == _us(2025, 7, 6, 12, 0, 0)


def test_value_borne_offset_is_honoured_as_parsed(store) -> None:
    # The old test_timezone case: an ISO value carrying its own +02:00 needs no
    # configured zone — the applied offset is the value's own.
    bundle = _bundle(store, "aware", "instant", "iso8601",
                     [("lower", None, "2025-06-06T13:00:00+02:00")])
    integrate_expression(store, bundle, [TzLocaliseMethod()])
    row = _row(store, bundle)
    assert float(row["utc_offset_hours_lower"]) == 2.0
    assert row["lower_unix_utc_us"] == _us(2025, 6, 6, 11, 0, 0)
    assert row["lower_datetime_iso"] == "2025-06-06T13:00:00.000000+02:00"


# --- Verify bullet 2: assumed-UTC -> loud N/A ---------------------------------

def test_assumed_utc_flags_offset_and_iso_loudly(store) -> None:
    bundle = _bundle(store, "naive", "instant", "iso8601",
                     [("lower", None, "2026-03-14T02:20:31")])
    integrate_expression(store, bundle, [AssumeUtcMethod()])
    row = _row(store, bundle)
    assert row["utc_offset_hours_lower"] == V.UTC_OFFSET_UNKNOWN   # the sentinel, not 0.0
    assert row["utc_offset_hours_upper"] == V.UTC_OFFSET_UNKNOWN   # instant mirrors
    assert row["lower_datetime_iso"] == "2026-03-14T02:20:31.000000 N/A"
    assert row["lower_datetime_iso"].endswith(" N/A")
    assert row["lower_unix_utc_us"] == _us(2026, 3, 14, 2, 20, 31)
    assert row["method"] == "assume_utc/1.0"


def test_assumed_na_is_distinct_from_empty(store) -> None:
    # D32: empty = no value; ` N/A` = value with unknown zone. The two must never
    # collapse — an assumed instant has FULL cells, an absent bound has EMPTY ones.
    assumed = _bundle(store, "na-vs-empty", "instant", "local_string",
                      [("lower", None, "14.03.2026 02:20:31")])
    integrate_expression(
        store, assumed, [AssumeUtcMethod(datetime_format="%d.%m.%Y %H:%M:%S")])
    row = _row(store, assumed)
    assert row["lower_datetime_iso"].endswith(" N/A") and row["lower_datetime_iso"] != ""
    assert row["lower_date"] is not None and row["lower_time"] is not None


# --- Verify bullet 3: an absent bound -> empty cells --------------------------

def test_absent_upper_bound_yields_empty_cells(store) -> None:
    # An interval whose upper never existed: the upper cells are EMPTY (empty ISO
    # string, NULL date/time/unix/offset) — not N/A, and nothing invented.
    bundle = _bundle(store, "open", "interval", "unix_epoch_s",
                     [("lower", None, "1773450680")])
    integrate_expression(store, bundle, epoch_methods())
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == 1_773_450_680_000_000
    assert row["upper_unix_utc_us"] is None
    assert row["upper_datetime_iso"] == ""          # empty, NOT " N/A"
    assert row["upper_date"] is None
    assert row["upper_time"] is None
    assert row["utc_offset_hours_upper"] is None


def test_time_only_split_bound_yields_empty_cells(store) -> None:
    # A time-of-day with no date column cannot anchor an instant: that bound stays
    # empty rather than inventing a date; the dated bound still integrates.
    bundle = _bundle(store, "timeonly", "interval", "local_string", [
        ("lower_date", None, "2026-03-14"),
        ("lower_time", None, "02:20:31"),
        ("upper_time", None, "03:00:00"),           # no upper_date -> no upper instant
    ])
    integrate_expression(store, bundle, [DatetimeCombineMethod()])
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == _us(2026, 3, 14, 2, 20, 31)
    assert row["upper_unix_utc_us"] is None
    assert row["upper_datetime_iso"] == ""
    assert row["upper_date"] is None and row["upper_time"] is None


# --- Verify bullet 4: the epoch constants live in the method string -----------

def test_apple_cfabsolute_decodes_with_constant_in_method(store) -> None:
    bundle = _bundle(store, "cf", "instant", "apple_cfabsolute",
                     [("lower", None, "0")])
    integrate_expression(store, bundle, epoch_methods())
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == 978_307_200_000_000   # 2001-01-01T00:00:00Z
    assert row["method"] == "epoch_cfabsolute/1.0/offset_s=978307200"
    # An epoch is absolute UTC: nothing assumed, offset not applicable, exact UTC ISO.
    assert row["utc_offset_hours_lower"] is None
    assert row["lower_datetime_iso"] == "2001-01-01T00:00:00.000000+00:00"


def test_webkit_decodes_with_constant_in_method(store) -> None:
    # 11 644 473 600 s of µs after 1601-01-01 IS the Unix epoch.
    bundle = _bundle(store, "wk", "instant", "webkit",
                     [("lower", None, "11644473600000000")])
    integrate_expression(store, bundle, epoch_methods())
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == 0
    assert row["method"] == "epoch_webkit/1.0/offset_s=-11644473600"


def test_filetime_decodes_with_constant_in_method(store) -> None:
    # FILETIME counts 100 ns ticks from 1601: the same anchor, ten ticks per µs.
    bundle = _bundle(store, "ft", "instant", "filetime",
                     [("lower", None, "116444736000000000")])
    integrate_expression(store, bundle, epoch_methods())
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == 0
    assert row["method"] == "epoch_filetime/1.0/offset_s=-11644473600"


def test_excel_serial_decodes_as_assumed_clock_with_epoch_in_method(store) -> None:
    # Serial 25569 days after 1899-12-30 = 1970-01-01; a serial is a zone-less WALL
    # CLOCK, so unlike the true epochs it decodes assumed-UTC (sentinel + ` N/A`).
    bundle = _bundle(store, "xl", "instant", "excel_serial",
                     [("lower", None, "25569.5")])
    integrate_expression(store, bundle, epoch_methods())
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == _US_PER_DAY // 2      # 1970-01-01 12:00 assumed
    assert row["method"] == "epoch_excel_serial/1.0/epoch=1899-12-30"
    assert row["utc_offset_hours_lower"] == V.UTC_OFFSET_UNKNOWN
    assert row["lower_datetime_iso"].endswith(" N/A")


def test_unix_scale_decodes_and_leaves_accuracy_unset(store) -> None:
    ms = _bundle(store, "ms", "instant", "unix_epoch_ms",
                 [("lower", None, "1773450680123")])
    us = _bundle(store, "us", "instant", "unix_epoch_us",
                 [("lower", None, "1773450680123456")])
    integrate_expression(store, ms, epoch_methods())
    integrate_expression(store, us, epoch_methods())
    ms_row, us_row = _row(store, ms), _row(store, us)
    assert ms_row["lower_unix_utc_us"] == 1_773_450_680_123_000
    assert us_row["lower_unix_utc_us"] == 1_773_450_680_123_456
    # Accuracy is NOT the clock's resolution: an epoch decode invents none (source-stated).
    assert ms_row["lower_accuracy_us"] is None and ms_row["upper_accuracy_us"] is None
    assert us_row["lower_accuracy_us"] is None and us_row["upper_accuracy_us"] is None
    # A pass-through is a method too: the µs rule still stamps itself.
    assert us_row["method"] == "epoch_unix_us/1.0"


# --- datetime_combine ----------------------------------------------------------

def test_datetime_combine_without_zone_assumes_utc(store) -> None:
    bundle = _bundle(store, "combine", "instant", "local_string", [
        ("lower_date", None, "2026-03-14"),
        ("lower_time", None, "02:20:31"),
    ])
    integrate_expression(store, bundle, [DatetimeCombineMethod()])
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == _us(2026, 3, 14, 2, 20, 31)
    assert row["method"] == "datetime_combine/1.0+assume_utc/1.0"
    assert row["utc_offset_hours_lower"] == V.UTC_OFFSET_UNKNOWN
    assert row["lower_datetime_iso"].endswith(" N/A")


def test_datetime_combine_with_zone_localises_and_stamps_tzdb(store) -> None:
    bundle = _bundle(store, "combine-tz", "instant", "local_string", [
        ("lower_date", None, "2026-03-14"),
        ("lower_time", None, "02:20:31"),
    ])
    integrate_expression(store, bundle, [DatetimeCombineMethod(zone="Europe/Paris")])
    row = _row(store, bundle)
    assert float(row["utc_offset_hours_lower"]) == 1.0        # March: Paris winter
    assert row["lower_unix_utc_us"] == _us(2026, 3, 14, 1, 20, 31)
    assert row["lower_datetime_iso"] == "2026-03-14T02:20:31.000000+01:00"
    assert row["method"].startswith("datetime_combine/1.0+tz_localize/1.0/tzdb=")


def test_datetime_combine_date_only_decodes_at_midnight_without_inventing_accuracy(store) -> None:
    bundle = _bundle(store, "dateonly", "instant", "local_string",
                     [("lower_date", None, "2026-03-14")])
    integrate_expression(store, bundle, [DatetimeCombineMethod(zone="Europe/Paris")])
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == _us(2026, 3, 13, 23, 0, 0)   # local midnight
    # A bare date decodes at midnight but claims NO accuracy (day-granularity is
    # representation, not a stated uncertainty).
    assert row["lower_accuracy_us"] is None and row["upper_accuracy_us"] is None


# --- source-stated per-bound accuracy (T97 plumbing) ---------------------------

def test_source_stated_accuracy_reaches_each_bound_column_verbatim(store) -> None:
    # A preset that maps a per-bound accuracy lands it as a `<side>_accuracy` component;
    # integrate reads it VERBATIM into the matching L2 column, independently per bound.
    bundle = _bundle(store, "acc", "interval", "unix_epoch_s", [
        ("lower", None, "1773450680"),
        ("upper", None, "1773454280"),
        ("lower_accuracy", None, "1000000"),        # source states ±1 s on the start
        ("upper_accuracy", None, "86400000000"),    # ±1 day on the end
    ])
    integrate_expression(store, bundle, epoch_methods())
    row = _row(store, bundle)
    assert row["lower_accuracy_us"] == 1_000_000
    assert row["upper_accuracy_us"] == 86_400_000_000


def test_instant_accuracy_carries_onto_the_degenerate_upper_bound(store) -> None:
    # An instant is one reading: its lower accuracy carries onto the collapsed upper bound.
    bundle = _bundle(store, "acc-inst", "instant", "unix_epoch_s", [
        ("lower", None, "1773450680"),
        ("lower_accuracy", None, "500000"),
    ])
    integrate_expression(store, bundle, epoch_methods())
    row = _row(store, bundle)
    assert row["lower_accuracy_us"] == 500_000
    assert row["upper_accuracy_us"] == 500_000


def test_source_stated_accuracy_accepts_a_fractional_value_rounded_to_us(store) -> None:
    # A source stating a fractional µs count is rounded to whole µs (the INTEGER column).
    bundle = _bundle(store, "acc-frac", "instant", "unix_epoch_s", [
        ("lower", None, "1773450680"),
        ("lower_accuracy", None, "1500000.4"),
    ])
    integrate_expression(store, bundle, epoch_methods())
    assert _row(store, bundle)["lower_accuracy_us"] == 1_500_000


def test_unreadable_source_accuracy_is_dropped_not_invented(store) -> None:
    # A non-numeric accuracy cell warns and yields None — one bad cell never sinks the
    # expression, and nothing is invented in its place (no-silent-loss discipline).
    bundle = _bundle(store, "acc-bad", "instant", "unix_epoch_s", [
        ("lower", None, "1773450680"),
        ("lower_accuracy", None, "about a second"),
    ])
    integrate_expression(store, bundle, epoch_methods())
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == 1_773_450_680_000_000   # the bound still decodes
    assert row["lower_accuracy_us"] is None                    # the bad accuracy is dropped


def test_source_stated_accuracy_overrides_the_method_level_claim(store) -> None:
    # A Method may carry its OWN accuracy claim (a fuzzy-time reader); a source-stated
    # per-bound value is stronger evidence and wins.
    bundle = _bundle(store, "acc-ovr", "instant", "local_string", [
        ("lower", None, "2026-03-14T12:00:00+01:00"),
        ("lower_accuracy", None, "250000"),
    ])
    method = AssumeUtcMethod()
    method.time_accuracy_us = 3_600_000_000          # the method's fallback (±1 h)
    integrate_expression(store, bundle, [method])
    row = _row(store, bundle)
    assert row["lower_accuracy_us"] == 250_000       # the source-stated value wins


# --- D32 cells + identity + dispatch --------------------------------------------

def test_local_date_and_time_cells_derive_from_the_applied_offset(store) -> None:
    # Local 01:30+01:00 is 00:30 UTC on the 14th: the date cell is the LOCAL
    # calendar day and the time cell the µs past LOCAL midnight (never the UTC ones).
    bundle = _bundle(store, "cells", "instant", "iso8601",
                     [("lower", None, "2026-03-14T01:30:00+01:00")])
    integrate_expression(store, bundle, [TzLocaliseMethod()])
    row = _row(store, bundle)
    assert row["lower_date"] == _us(2026, 3, 14, 0, 0, 0)
    assert row["lower_time"] == 90 * 60 * 1_000_000            # 01:30 local
    assert row["lower_datetime_iso"] == "2026-03-14T01:30:00.000000+01:00"


def test_instant_mirrors_the_single_bound(store) -> None:
    bundle = _bundle(store, "mirror", "instant", "unix_epoch_s",
                     [("lower", None, "1773450680")])
    integrate_expression(store, bundle, epoch_methods())
    row = _row(store, bundle)
    assert row["upper_unix_utc_us"] == row["lower_unix_utc_us"]
    assert row["upper_datetime_iso"] == row["lower_datetime_iso"]


def test_integration_id_derives_from_core_ids(store) -> None:
    bundle = _bundle(store, "idcheck", "instant", "unix_epoch_s",
                     [("lower", None, "1773450680")])
    written = integrate_expression(store, bundle, epoch_methods())
    assert written is not None
    assert written.timespan_integration_id == ids.timespan_integration_id(
        bundle.expression.temporal_expression_id, written.method)


def test_standard_methods_dispatch_by_referential(store) -> None:
    # The default ordered set routes each referential to its named rule; the
    # zone-less naive clock deliberately falls through to assume_utc, last.
    methods = standard_methods()
    epoch = _bundle(store, "d-epoch", "instant", "unix_epoch_ms",
                    [("lower", None, "1773450680123")])
    naive = _bundle(store, "d-naive", "instant", "iso8601",
                    [("lower", None, "2026-03-14T02:20:31")])
    split = _bundle(store, "d-split", "instant", "local_string", [
        ("lower_date", None, "2026-03-14"), ("lower_time", None, "02:20:31")])
    assert integrate_expression(store, epoch, methods).method == "epoch_unix_ms/1.0"
    assert integrate_expression(store, naive, methods).method == "assume_utc/1.0"
    assert integrate_expression(store, split, methods).method \
        == "datetime_combine/1.0+assume_utc/1.0"


# --- Verify bullet 5: every integration row carries a method --------------------

def test_every_integration_row_has_a_nonempty_method(store) -> None:
    # One expression per rule family, integrated through the default set: EVERY
    # written row must be method-stamped (the L2 invariant — a pass-through too).
    methods = standard_methods(zone="Europe/Paris")
    for key, referential, components in (
        ("m1", "unix_epoch_s", [("lower", None, "1773450680")]),
        ("m2", "apple_cfabsolute", [("lower", None, "763689600")]),
        ("m3", "webkit", [("lower", None, "13391164800000000")]),
        ("m4", "excel_serial", [("lower", None, "45723")]),
        ("m5", "iso8601", [("lower", None, "2026-03-14T02:20:31+01:00")]),
        ("m6", "local_string", [("lower_date", None, "2026-03-14"),
                                ("lower_time", None, "02:20:31")]),
    ):
        integrate_expression(store, _bundle(store, key, "instant", referential,
                                            components), methods)
    rows = store.connection.execute(
        "SELECT method FROM timespan_integration").fetchall()
    assert len(rows) == 6
    assert all(method and method.strip() for (method,) in rows)
    # rule + version are always present: "rule/version[...]".
    assert all(len(method.split("/")) >= 2 for (method,) in rows)
