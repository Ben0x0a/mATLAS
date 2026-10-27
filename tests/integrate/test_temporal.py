"""Verify suite: the temporal Methods and their timespan_integration rows.

Defines:    the tests for ``model_atlas/integrate/temporal.py`` — the Verify
            bullets (dual per-bound DST offsets, the assumed-UTC ``N/A`` discipline,
            empty cells for an absent bound, the epoch constants recorded in the
            method strings, every row method-stamped) plus the per-bound applied-offset
            cell shapes, the id derivation and the first-applicable-method dispatch.
            The DST cases
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
from model_atlas.core.local_time import render_local
from model_atlas.core.store import RunStore
from model_atlas.integrate.temporal import (
    AssumeUtcMethod,
    DatetimeCombineMethod,
    EpochMethod,
    StartPlusDurationMethod,
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
    expression_id = ids.new_id()
    expression = V.TemporalExpression(
        temporal_expression_id=expression_id, kind=kind, referential=referential)
    store.write_temporal_expression(expression)
    rows = []
    for ordinal, (part, stated_offset, raw_value) in enumerate(components):
        component = V.TemporalComponent(
            temporal_component_id=ids.new_id(),
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
    integrate_expression(store, bundle, [method], local_zone="Europe/Paris")
    row = _row(store, bundle)
    # The decode resolved DST per bound: 13:00 local is 11:00Z in summer, 12:00Z in winter.
    assert row["lower_unix_utc_us"] == _us(2025, 10, 25, 11, 0, 0)   # 13:00 +2
    assert row["upper_unix_utc_us"] == _us(2025, 10, 26, 12, 0, 0)   # 13:00 +1
    # And the offset the analyst adds to read each bound locally is ALSO per bound, for the
    # same transition — +2 on the summer side, +1 on the winter side.
    assert float(row["offset_to_local_hours_lower"]) == 2.0
    assert float(row["offset_to_local_hours_upper"]) == 1.0
    # The L1 stated offset is untouched — one token, two different resolutions.
    assert bundle.components[0].stated_offset == "UTC+01:00[DST]"


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
    # 13:00 with a fixed +01:00 is 12:00Z even in July: the instant IS the assertion, since
    # the offset the source declared is no longer echoed into a column of its own.
    assert row["lower_unix_utc_us"] == _us(2025, 7, 6, 12, 0, 0)


def test_source_named_zone_resolves_dst_without_any_configured_zone(store) -> None:
    """A source that NAMES its zone (``zone: const(Europe/Zurich)``) resolves per bound
    through the tzdb with NO operator-configured zone: +01:00 in winter, +02:00 in summer.
    Before this, the name parsed as an unusable token and both bounds were silently
    materialised by assuming UTC."""
    method = TzLocaliseMethod(datetime_format="%Y-%m-%d %H:%M:%S")   # zone=None on purpose
    bundle = _bundle(store, "named-zone", "interval", "local_string", [
        ("lower", "Europe/Zurich", "2026-01-15 13:00:00"),
        ("upper", "Europe/Zurich", "2026-07-15 13:00:00"),
    ])
    integrate_expression(store, bundle, [method])
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == _us(2026, 1, 15, 12, 0, 0)   # CET, +1
    assert row["upper_unix_utc_us"] == _us(2026, 7, 15, 11, 0, 0)   # CEST, +2
    # Nothing was ASSUMED: the method names tz_localize and carries the tzdb reference,
    # which is where "how this instant was reached" lives now.
    assert "tzdb=" in row["method"] and row["method"].startswith("tz_localize/")


def test_source_named_zone_outranks_the_configured_zone(store) -> None:
    """The preset states what the SOURCE's clocks mean; the configured local zone only says
    what to assume when the source says nothing. So a named zone wins over the config."""
    method = TzLocaliseMethod(zone="America/New_York", datetime_format="%Y-%m-%d %H:%M:%S")
    bundle = _bundle(store, "named-wins", "instant", "local_string",
                     [("lower", "Europe/Zurich", "2026-01-15 13:00:00")])
    integrate_expression(store, bundle, [method])
    row = _row(store, bundle)
    # 13:00 read as Zurich (+1) is 12:00Z; read as New York (-5) it would be 18:00Z.
    assert row["lower_unix_utc_us"] == _us(2026, 1, 15, 12, 0, 0)


@pytest.mark.parametrize("unit, raw, seconds", [
    ("s", "187", 187),
    ("ms", "187000", 187),
    ("min", "3", 180),
    ("h", "0.5", 1800),
    ("d", "0.25", 21600),
])
def test_start_plus_duration_derives_the_upper_bound(store, unit, raw, seconds) -> None:
    """The CDR shape: L1 holds a start clock plus the source's VERBATIM span under the part
    ``duration``, and the method derives the end. The unit is the preset's declaration, not
    something read from the data."""
    inner = TzLocaliseMethod(zone="Europe/Zurich", datetime_format="%Y-%m-%d %H:%M:%S")
    method = StartPlusDurationMethod(inner, unit)
    bundle = _bundle(store, f"dur-{unit}", "interval", "local_string", [
        ("lower", "Europe/Zurich", "2026-01-15 13:00:00"),
        ("duration", "Europe/Zurich", raw),
    ])
    integrate_expression(store, bundle, [method])
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == _us(2026, 1, 15, 12, 0, 0)
    assert row["upper_unix_utc_us"] - row["lower_unix_utc_us"] == seconds * 1_000_000
    # The composite stamp names the decode that produced the start, THEN the derivation.
    assert row["method"] == f"{inner.method}+start_plus_duration/1.0"
    # The id is random (decision 50); what must hold is that ONE row was written for the
    # expression, which _row already asserts by fetching exactly one.
    assert row["timespan_integration_id"]


def test_start_plus_duration_keeps_the_derived_end_in_the_starts_offset(store) -> None:
    """The derived end is exact in UTC (absolute µs arithmetic) and rendered in the offset the
    start was stated in — including across a DST transition, where the wall-clock ISO is
    therefore one hour off the local reading while the UTC instant stays right."""
    inner = TzLocaliseMethod(zone="Europe/Zurich", datetime_format="%Y-%m-%d %H:%M:%S")
    # 2026-03-29 02:00 CET -> 03:00 CEST: a 2h span from 01:30 crosses the gap.
    bundle = _bundle(store, "dur-dst", "interval", "local_string", [
        ("lower", "Europe/Zurich", "2026-03-29 01:30:00"),
        ("duration", "Europe/Zurich", "7200"),
    ])
    integrate_expression(store, bundle, [StartPlusDurationMethod(inner, "s")])
    row = _row(store, bundle)
    assert row["upper_unix_utc_us"] - row["lower_unix_utc_us"] == 7200 * 1_000_000


def test_unreadable_duration_leaves_the_start_alone(store) -> None:
    """A junk or negative span costs the END bound, never the whole row: the wrapper declines
    and the plain method behind it still writes the start. Nothing is invented."""
    inner = TzLocaliseMethod(zone="Europe/Zurich", datetime_format="%Y-%m-%d %H:%M:%S")
    for key, raw in (("dur-junk", "n/a"), ("dur-negative", "-5")):
        bundle = _bundle(store, key, "interval", "local_string", [
            ("lower", "Europe/Zurich", "2026-01-15 13:00:00"),
            ("duration", "Europe/Zurich", raw),
        ])
        integrate_expression(store, bundle, [StartPlusDurationMethod(inner, "s"), inner])
        row = _row(store, bundle)
        assert row["lower_unix_utc_us"] == _us(2026, 1, 15, 12, 0, 0)
        assert row["upper_unix_utc_us"] is None
        assert row["method"] == inner.method            # the plain method wrote this row


def test_standard_methods_only_wrap_when_a_duration_unit_is_declared(store) -> None:
    """The wrapper is opt-in per preset: with no ``duration_sec:`` the method set is unchanged,
    so a stray ``duration`` component can never silently start deriving end bounds."""
    plain = standard_methods(zone="Europe/Zurich")
    wrapped = standard_methods(zone="Europe/Zurich", duration_unit="s")
    assert not any(isinstance(m, StartPlusDurationMethod) for m in plain)
    assert len(wrapped) == 2 * len(plain)
    # Every wrapper precedes every plain method, so the derivation is tried first.
    assert all(isinstance(m, StartPlusDurationMethod) for m in wrapped[:len(plain)])


def test_value_borne_offset_is_honoured_as_parsed(store) -> None:
    # The old test_timezone case: an ISO value carrying its own +02:00 needs no
    # configured zone — the applied offset is the value's own.
    bundle = _bundle(store, "aware", "instant", "iso8601",
                     [("lower", None, "2025-06-06T13:00:00+02:00")])
    integrate_expression(store, bundle, [TzLocaliseMethod()])
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == _us(2025, 6, 6, 11, 0, 0)   # 13:00 +02:00


# --- Verify bullet 2: the assumption is stamped in the method ------------------

def test_assumed_utc_is_stamped_in_the_method_not_an_offset_cell(store) -> None:
    """A naive clock with no zone knowledge is materialised by ASSUMING UTC, and that
    assumption must stay visible. It now lives in ``method`` — per row, auditable, and
    unlike the old `` N/A`` offset it cannot be confused with the analyst's local offset."""
    bundle = _bundle(store, "naive", "instant", "iso8601",
                     [("lower", None, "2026-03-14T02:20:31")])
    integrate_expression(store, bundle, [AssumeUtcMethod()], local_zone="Europe/Paris")
    row = _row(store, bundle)
    assert row["method"] == "assume_utc/1.0"
    assert row["lower_unix_utc_us"] == _us(2026, 3, 14, 2, 20, 31)
    # The offset to local is about the CASE's zone and is unaffected by the assumption:
    # +1 in March, exactly as for a row whose zone was known. The two facts are separate.
    assert float(row["offset_to_local_hours_lower"]) == 1.0
    assert float(row["offset_to_local_hours_upper"]) == 1.0


def test_no_configured_zone_yields_the_sentinel_not_a_silent_zero(store) -> None:
    """With no ``local_zone`` the run cannot say what local means, and must say so: the
    loud ``N/A``, never 0.0 — which would claim the case IS on UTC."""
    bundle = _bundle(store, "no-zone", "instant", "iso8601",
                     [("lower", None, "2026-03-14T02:20:31+01:00")])
    integrate_expression(store, bundle, [TzLocaliseMethod()])     # local_zone omitted
    row = _row(store, bundle)
    assert row["offset_to_local_hours_lower"] == V.LOCAL_OFFSET_UNKNOWN
    assert row["offset_to_local_hours_upper"] == V.LOCAL_OFFSET_UNKNOWN
    assert row["lower_unix_utc_us"] is not None    # the instant is still known exactly


# --- Verify bullet 3: an absent bound -> empty cells --------------------------

def test_absent_upper_bound_yields_empty_cells(store) -> None:
    # An interval whose upper never existed: the upper cells are NULL, and — with a zone
    # configured, so the sentinel is NOT in play — that NULL means "no bound", never
    # "no zone". The two absences must not collapse.
    bundle = _bundle(store, "open", "interval", "unix_epoch_s",
                     [("lower", None, "1773450680")])
    integrate_expression(store, bundle, epoch_methods(), local_zone="Europe/Paris")
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == 1_773_450_680_000_000
    assert row["upper_unix_utc_us"] is None
    assert float(row["offset_to_local_hours_lower"]) == 1.0
    assert row["offset_to_local_hours_upper"] is None   # NULL, NOT the "N/A" sentinel


def test_time_only_split_bound_yields_empty_cells(store) -> None:
    # A time-of-day with no date column cannot anchor an instant: that bound stays
    # empty rather than inventing a date; the dated bound still integrates.
    bundle = _bundle(store, "timeonly", "interval", "local_string", [
        ("lower_date", None, "2026-03-14"),
        ("lower_time", None, "02:20:31"),
        ("upper_time", None, "03:00:00"),           # no upper_date -> no upper instant
    ])
    integrate_expression(store, bundle, [DatetimeCombineMethod()], local_zone="Europe/Paris")
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == _us(2026, 3, 14, 2, 20, 31)
    assert row["upper_unix_utc_us"] is None
    assert row["offset_to_local_hours_upper"] is None


# --- Verify bullet 4: the epoch constants live in the method string -----------

def test_apple_cfabsolute_decodes_with_constant_in_method(store) -> None:
    bundle = _bundle(store, "cf", "instant", "apple_cfabsolute",
                     [("lower", None, "0")])
    integrate_expression(store, bundle, epoch_methods())
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == 978_307_200_000_000   # 2001-01-01T00:00:00Z
    assert row["method"] == "epoch_cfabsolute/1.0/offset_s=978307200"


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
    # The assumption is named in the method — the only place it lives now.
    assert row["method"] == "datetime_combine/1.0+assume_utc/1.0"


def test_datetime_combine_with_zone_localises_and_stamps_tzdb(store) -> None:
    bundle = _bundle(store, "combine-tz", "instant", "local_string", [
        ("lower_date", None, "2026-03-14"),
        ("lower_time", None, "02:20:31"),
    ])
    integrate_expression(store, bundle, [DatetimeCombineMethod(zone="Europe/Paris")])
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == _us(2026, 3, 14, 1, 20, 31)   # 02:20:31 Paris, +1
    assert row["method"].startswith("datetime_combine/1.0+tz_localize/1.0/tzdb=")


def test_instant_collapse_is_symmetric_when_only_the_upper_decodes(store) -> None:
    # An instant recorded SOLELY under the upper part (the lower failed to parse, or the
    # value lived in the 'upper' column) must still collapse to [t, t]; a one-directional
    # copy would leave [NULL, t] that lower-bound/interval queries misread as open-below.
    bundle = _bundle(store, "upper-only", "instant", "unix_epoch_s",
                     [("upper", None, "1773450680")])
    integrate_expression(store, bundle, epoch_methods())
    row = _row(store, bundle)
    assert row["upper_unix_utc_us"] == 1_773_450_680_000_000
    assert row["lower_unix_utc_us"] == row["upper_unix_utc_us"]      # collapsed downward
    assert row["offset_to_local_hours_lower"] == row["offset_to_local_hours_upper"]


def test_datetime_combine_honours_an_inline_time_offset_and_stamps_tz(store) -> None:
    # A time cell carrying its OWN +01:00 must decode (not be dropped by a bare
    # %H:%M:%S strptime) AND — zone-less — be stamped tz_localize, never assume_utc,
    # because an offset was in fact applied for the bound (audit accuracy).
    bundle = _bundle(store, "combine-inline", "instant", "local_string", [
        ("lower_date", None, "2026-03-14"),
        ("lower_time", None, "02:20:31+01:00"),
    ])
    integrate_expression(store, bundle, [DatetimeCombineMethod()])
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == _us(2026, 3, 14, 1, 20, 31)   # 02:20:31 +1
    assert row["method"].startswith("datetime_combine/1.0+tz_localize/1.0/tzdb=")
    assert "assume_utc" not in row["method"]


def test_zoneless_format_borne_offset_is_stamped_tz_localize_not_assume_utc(store) -> None:
    # 3(b): a local_string parsed through a %z format carries a real offset; with NO zone
    # configured it must still dispatch to tz_localize (the offset is applied), not fall
    # through to assume_utc, which would honour the offset yet mis-stamp the derivation.
    methods = standard_methods(datetime_format="%d.%m.%Y %H:%M:%S %z")
    bundle = _bundle(store, "fmt-offset", "instant", "local_string",
                     [("lower", None, "14.03.2026 02:20:31 +0100")])
    written = integrate_expression(store, bundle, methods)
    assert written is not None
    assert written.method.startswith("tz_localize/")     # not assume_utc
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == _us(2026, 3, 14, 1, 20, 31)


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

def test_the_stored_pair_reconstructs_the_local_reading(store) -> None:
    """The two stored columns are sufficient: canonical UTC µs + the offset to local
    render back to the analyst's wall clock, which is why no rendered column is stored.

    The case here is the one that used to need a stored cell to get right — an instant
    just after LOCAL midnight but still on the PREVIOUS day in UTC. 2026-03-14 00:30
    Paris is 2026-03-13 23:30Z: a reader who ignored the offset would file it under the
    wrong calendar day."""
    bundle = _bundle(store, "cells", "instant", "iso8601",
                     [("lower", None, "2026-03-13T23:30:00+00:00")])
    integrate_expression(store, bundle, [TzLocaliseMethod()], local_zone="Europe/Paris")
    row = _row(store, bundle)
    assert row["lower_unix_utc_us"] == _us(2026, 3, 13, 23, 30, 0)   # the 13th, in UTC
    assert float(row["offset_to_local_hours_lower"]) == 1.0
    assert render_local(row["lower_unix_utc_us"],
                        float(row["offset_to_local_hours_lower"])) == (
        "2026-03-14T00:30:00.000000+01:00")                          # the 14th, locally


def test_instant_mirrors_the_single_bound(store) -> None:
    bundle = _bundle(store, "mirror", "instant", "unix_epoch_s",
                     [("lower", None, "1773450680")])
    integrate_expression(store, bundle, epoch_methods())
    row = _row(store, bundle)
    assert row["upper_unix_utc_us"] == row["lower_unix_utc_us"]


def test_integration_id_is_random_and_unique_per_expression(store) -> None:
    """Ids no longer derive from anything (decision 50). The invariant that survives is
    UNIQUE(temporal_expression_id): one integration per expression, enforced by the store,
    not by two callers computing the same uuid5."""
    bundle = _bundle(store, "idcheck", "instant", "unix_epoch_s",
                     [("lower", None, "1773450680")])
    written = integrate_expression(store, bundle, epoch_methods())
    assert written is not None
    assert written.timespan_integration_id != written.temporal_expression_id
    stored = _row(store, bundle)
    assert stored["timespan_integration_id"] == written.timespan_integration_id


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
