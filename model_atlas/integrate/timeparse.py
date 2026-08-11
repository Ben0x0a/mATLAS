"""Pure zone / ISO-8601 timestamp primitives for the L2 temporal methods.

Defines:    ``tz_offset_to_hours`` (offset token -> signed hours), ``ZoneToken`` +
            ``parse_zone_token`` (source zone declarations: ``UTC+01:00`` /
            ``UTC+01:00[DST]`` / ``local`` / a named IANA zone) + ``is_known_zone``
            (the cached tzdb membership test), ``zone_standard_offset_hours`` (a zone's
            non-DST offset), ``local_naive_to_utc_us`` (DST-aware naive-local ->
            UTC µs with the ambiguous/imaginary anomaly), ``datetime_to_us`` (aware
            datetime -> Unix µs) and ``parse_iso8601``.
Used by:    integrate.temporal (the zone ladder of the tz-localise / assume-UTC /
            datetime-combine methods).
Depends on: standard library (datetime, re, zoneinfo); dateutil optionally, as an
            ISO-8601 fallback parser.

Moved verbatim from ``transforms/builtin.py`` — these are the
decode semantics the L2 temporal methods rely on, so they live beside their consumer
in ``integrate`` (they are NOT preset-pipe machinery; the pipe transforms moved to
``presets/pipes.py`` instead). The old module's epoch decoders
(``epoch_to_us``/``parse_datetime_to_us``) and the display-zone helper
(``zone_offset_hours_at``) are superseded by the named ``integrate.temporal``
methods and are deliberately not carried over.

All datetime arithmetic here is timedelta-from-epoch (never ``fromtimestamp``): on
Windows the CRT raises OSError for pre-1970 / far-future instants that occur in real
forensic data; pure timedelta arithmetic is valid across the whole datetime range.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

_UNIX_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)

_TZ_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})?$")


def tz_offset_to_hours(value: Any) -> float:
    """Normalise a timezone offset (number or string) to signed decimal hours."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    token = str(value).strip().upper()
    if token in ("", "UTC", "GMT", "Z"):
        return 0.0
    for prefix in ("UTC", "GMT"):
        if token.startswith(prefix):
            token = token[len(prefix):]
            break
    match = _TZ_OFFSET_RE.match(token)
    if match is None:
        raise ValueError(f"Unrecognised timezone offset {value!r}")
    sign = 1 if match.group(1) == "+" else -1
    return sign * (int(match.group(2)) + int(match.group(3) or 0) / 60)


@dataclass(frozen=True)
class ZoneToken:
    """A source zone declaration parsed from a captured header/const token.

    ``base_offset_hours`` is the fixed/standard offset (None for ``local`` or unparseable);
    ``dst`` is True when the source applies daylight saving (AXIOM's ``[DST]`` marker, the
    ``local`` keyword, or a named IANA zone) — i.e. the per-row offset varies and a single
    number is not enough. ``zone_name`` is the IANA zone the SOURCE named
    (``Europe/Zurich``), None for every other spelling; when set it is the authoritative
    resolver for this token's bounds."""

    base_offset_hours: float | None
    dst: bool
    raw: str | None
    zone_name: str | None = None


@lru_cache(maxsize=None)
def is_known_zone(name: str) -> bool:
    """Whether ``name`` is a zone the tz database knows (``Europe/Zurich``).

    HOW: attempt the ``ZoneInfo`` lookup — the same resolver that will later convert the
    bounds — and cache the verdict, since a preset repeats one zone on every row of a source.
    WHY the real lookup rather than a name-shape regex: only the tzdb can say whether a
    plausible-looking name actually resolves, and a name that does not resolve here would
    otherwise fail later, per record, at conversion time.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def parse_zone_token(value: Any) -> "ZoneToken | None":
    """Parse a source-zone token: ``UTC+01:00`` / ``UTC+01:00[DST]`` / ``local`` / an IANA
    zone name / a number.

    AXIOM writes the standard offset plus a ``[DST]`` suffix when daylight saving is applied;
    ``local`` means device-local (resolve via the configured zone); a named IANA zone is the
    source stating its OWN zone, which resolves per bound through the tzdb. None/empty -> None.
    """
    if value is None:
        return None
    token = str(value).strip()
    if not token:
        return None
    if token.casefold() == "local":
        return ZoneToken(base_offset_hours=None, dst=True, raw=token)
    dst = False
    core = token
    if core.upper().endswith("[DST]"):
        dst, core = True, core[:-5].strip()
    try:
        base = tz_offset_to_hours(core)
    except ValueError:
        # Not an offset — try the tzdb before giving up. WHY this branch matters: without it
        # a preset that names its zone (``zone: const(Europe/Zurich)``) yields an offsetless
        # token, the caller falls through to the CONFIGURED zone and, with none configured,
        # silently assumes UTC — the stated zone ignored, the error invisible on the row.
        if is_known_zone(core):
            return ZoneToken(base_offset_hours=zone_standard_offset_hours(core),
                             dst=True, raw=token, zone_name=core)
        return ZoneToken(base_offset_hours=None, dst=dst, raw=token)
    return ZoneToken(base_offset_hours=base, dst=dst, raw=token)


def zone_standard_offset_hours(zone_name: str) -> float:
    """The zone's STANDARD (non-DST) offset in hours — for the header-vs-config consistency
    check. Probes January and July and returns the offset when DST is not in effect."""
    from zoneinfo import ZoneInfo

    z = ZoneInfo(zone_name)
    for month in (1, 7):
        probe = dt.datetime(2025, month, 15, tzinfo=z)
        if probe.dst() == dt.timedelta(0):
            return probe.utcoffset().total_seconds() / 3600
    return dt.datetime(2025, 1, 15, tzinfo=z).utcoffset().total_seconds() / 3600


def local_naive_to_utc_us(naive: dt.datetime, zone_name: str) -> tuple[int, float, str | None]:
    """Convert a naive LOCAL datetime in ``zone_name`` to (unix_us, offset_hours, anomaly),
    DST-aware. ``anomaly`` is ``'ambiguous'`` (DST fall-back overlap — the wall-clock occurs
    twice; the earlier instant is chosen), ``'imaginary'`` (spring-forward gap — the
    wall-clock never existed), or None. The caller warns on a non-None anomaly."""
    from zoneinfo import ZoneInfo

    z = ZoneInfo(zone_name)
    aware = naive.replace(tzinfo=z)                       # fold=0 -> earlier instant at a fold
    anomaly: str | None = None
    if aware.utcoffset() != naive.replace(tzinfo=z, fold=1).utcoffset():
        # The offset depends on fold => a transition. Distinguish gap from overlap by a
        # round-trip: an imaginary (gap) time does not survive local->UTC->local.
        roundtrip = aware.astimezone(dt.timezone.utc).astimezone(z).replace(tzinfo=None)
        anomaly = "imaginary" if roundtrip != naive else "ambiguous"
    delta = aware.astimezone(dt.timezone.utc) - _UNIX_EPOCH
    unix_us = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    off = aware.utcoffset().total_seconds() / 3600
    return unix_us, off, anomaly


def datetime_to_us(parsed: dt.datetime) -> int:
    """Unix microseconds for an aware datetime (assumes tzinfo is set)."""
    delta = parsed - _UNIX_EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def parse_iso8601(value: Any) -> dt.datetime | None:
    """Parse an ISO-8601 string to a datetime (aware if it carries an offset, else naive).

    Handles the Cellebrite ``TimeStamp`` forms: zone-qualified (``...+00:00``, full ms) and
    naive (``DateTimeOnly``). A naive result is resolved against the preset ``zone:`` by the
    caller, never silently assumed UTC. Returns None for empty/unparseable input.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        try:  # tolerate variants (e.g. trailing 'Z' on older runtimes) when dateutil is present
            from dateutil import parser as _dateutil_parser

            return _dateutil_parser.isoparse(text)
        except Exception:  # noqa: BLE001 - unparseable: caller treats as missing
            return None
