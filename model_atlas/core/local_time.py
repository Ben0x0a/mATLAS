"""Render a canonical UTC instant as a local clock string, on demand.

Defines:    ``render_local(unix_us, offset_to_local_hours) -> str`` — the ONE way a
            human-readable time is produced from a ``timespan_integration`` row.
Used by:    ``model_atlas.core.verbalize`` (the spoken sentence) and
            ``model_atlas.analyse.concomitance.engine`` (the result rows' displayed
            times). Both formerly read a STORED ``lower_datetime_iso`` column.
Depends on: the standard library only — no store, no pandas, no vocabularies beyond the
            sentinel, so any layer may call it.

WHY this is a function and not a column
---------------------------------------
``timespan_integration`` used to store ``lower_datetime_iso`` / ``*_date`` / ``*_time``.
Storing them froze ONE zone's rendering into the evidence at ingest time, and a case
re-read under a different ``local_zone`` would still show the old clock while the
canonical µs said otherwise. The instant (``*_unix_utc_us``, always UTC) and the offset
to local (``offset_to_local_hours_*``) are the two facts; the string is a view of them,
recomputed wherever it is shown. Two readers must not each grow their own formatting, so
there is exactly one function.
"""
from __future__ import annotations

import datetime as dt

from model_atlas.core.vocabularies import LOCAL_OFFSET_UNKNOWN

_UNIX_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
_US_PER_HOUR = 3_600_000_000


def _offset_tag(hours: float) -> str:
    """``±HH:MM`` for an offset in hours (half-hour and 45-minute zones included)."""
    minutes = int(round(hours * 60))
    sign = "-" if minutes < 0 else "+"
    hh, mm = divmod(abs(minutes), 60)
    return f"{sign}{hh:02d}:{mm:02d}"


def render_local(unix_us: int | None, offset_hours: float | str | None) -> str:
    """One bound as a local, offset-tagged ISO string. ``""`` when there is no instant.

    Three outcomes, matching the stored offset's three states:
    - no instant                    -> ``""`` (EMPTY — never invented, and never "N/A":
      "no value" and "value whose zone we cannot name" must stay distinguishable);
    - a numeric offset              -> the local clock tagged ``±HH:MM``
      (``2026-03-14T02:20:31.000000+01:00``);
    - ``LOCAL_OFFSET_UNKNOWN``/None -> the UTC clock with a loud trailing `` N/A``, because
      no local zone is configured — the reader is seeing UTC and must know it is not local.

    All arithmetic is timedelta-from-epoch, never ``datetime.fromtimestamp``: the Windows
    CRT raises on the pre-1970 and far-future instants that occur in real evidence.
    """
    if unix_us is None:
        return ""
    numeric = isinstance(offset_hours, (int, float)) and not isinstance(offset_hours, bool)
    shift = float(offset_hours) if numeric else 0.0
    local = _UNIX_EPOCH + dt.timedelta(microseconds=unix_us + int(round(shift * _US_PER_HOUR)))
    clock = local.strftime("%Y-%m-%dT%H:%M:%S.%f")
    return clock + (_offset_tag(shift) if numeric else " " + LOCAL_OFFSET_UNKNOWN)
