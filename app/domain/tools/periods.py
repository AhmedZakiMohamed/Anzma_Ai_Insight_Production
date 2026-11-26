"""Building-local period and after-hours window arithmetic (ADR-0001 D-03, D-10).

Scope: only what ``doors.after_hours`` needs. The repo had no shared timezone/DST helper (``alert_engine`` only
converts an instant to a local hour; migration 010 falls back to ``Etc/GMT-2``), so this module is deliberately
small and pure. Reuse it from ``devices.offline_frequency`` instead of writing a second copy.

Rules implemented
* A period is either two ``YYYY-MM-DD`` building-local dates (end inclusive) or two ISO-8601 timestamps with an
  explicit offset (end exclusive). Naive timestamps and mixed forms are rejected: no guessing (D-03, G-TZ-02).
* Everything is half-open ``[start, end)``.
* Local midnight that does not exist (DST gap) resolves to the first valid instant of that day; local midnight
  that occurs twice resolves to its first occurrence (D-10). Any *other* local time that is nonexistent or
  ambiguous (e.g. an ``after_local`` of 02:30 on a transition day) is an error instead of a silent choice.
* Each local day D contributes the window ``[D after_local, D+1 local midnight)``; windows are clipped to the period.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_PERIOD_DAYS = 366
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME_RE = re.compile(r"(\d{2}):(\d{2})(?::(\d{2}))?")


class PeriodError(ValueError):
    """A parameter or timezone problem that must surface as CANNOT_COMPUTE with ``code`` (a LimitationCode value)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AfterHoursPeriod:
    timezone_name: str
    input_kind: str                                   # "date" or "timestamp"
    start_utc: datetime
    end_utc_exclusive: datetime
    after_local: time
    windows: tuple[tuple[datetime, datetime], ...]    # UTC, half-open, clipped to the period, ordered


def load_timezone(name: str | None) -> ZoneInfo:
    if not name or not name.strip():
        raise PeriodError("TIMEZONE_UNAVAILABLE", "The building has no timezone configured; local time cannot be computed.")
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise PeriodError("TIMEZONE_UNAVAILABLE", f"The building timezone {name!r} is not a valid installed IANA timezone.")


def parse_after_local(value: str | None) -> time:
    text = "20:00" if value is None else str(value).strip()
    m = _TIME_RE.fullmatch(text)
    if not m:
        raise PeriodError("INVALID_PARAMETER", "after_local must be a local time HH:MM or HH:MM:SS.")
    try:
        return time(int(m[1]), int(m[2]), int(m[3] or 0))
    except ValueError:
        raise PeriodError("INVALID_PARAMETER", "after_local is not a valid time of day.")


def resolve_local(day: date, at: time, tz: ZoneInfo, *, midnight_rule: bool) -> datetime:
    """Local wall time -> aware UTC instant. ``midnight_rule`` applies the D-10 gap/overlap rules; otherwise error."""
    naive = datetime.combine(day, at)
    first, second = naive.replace(tzinfo=tz, fold=0), naive.replace(tzinfo=tz, fold=1)
    if first.utcoffset() != second.utcoffset():
        if not midnight_rule:
            raise PeriodError("INVALID_PARAMETER",
                              f"Local time {naive.isoformat()} is nonexistent or ambiguous in {tz.key} (DST transition).")
        # gap: fold=0 uses the pre-transition offset, which lands exactly on the transition instant, i.e. the first
        # valid instant of the day; overlap: fold=0 is the first occurrence. Both are what D-10 asks for.
    return first.astimezone(timezone.utc)


def _midnight(day: date, tz: ZoneInfo) -> datetime:
    return resolve_local(day, time(0, 0), tz, midnight_rule=True)


def _aware(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise PeriodError("INVALID_PARAMETER", f"{label} is not a valid ISO-8601 date or timestamp.")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PeriodError("INVALID_PARAMETER", f"{label} has no UTC offset; supply an explicit offset such as +02:00 or Z.")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Period:
    """A building-local period converted to UTC, half-open ``[start_utc, end_utc_exclusive)`` (D-10)."""
    timezone_name: str
    input_kind: str                                   # "date" or "timestamp"
    start_utc: datetime
    end_utc_exclusive: datetime
    first_day: date                                   # building-local calendar days touched by the period
    last_day: date


def _resolve_bounds(start: str, end: str, tz: ZoneInfo) -> Period:
    s_date, e_date = bool(_DATE_RE.fullmatch(str(start).strip())), bool(_DATE_RE.fullmatch(str(end).strip()))
    if s_date != e_date:
        raise PeriodError("INVALID_PARAMETER", "start and end must both be dates (YYYY-MM-DD) or both timestamps with an offset.")

    if s_date:
        try:
            first_day, last_day = date.fromisoformat(start.strip()), date.fromisoformat(end.strip())
        except ValueError:
            raise PeriodError("INVALID_PARAMETER", "start/end is not a valid calendar date.")
        if last_day < first_day:
            raise PeriodError("INVALID_PARAMETER", "end must not be before start.")
        start_utc, end_utc, kind = _midnight(first_day, tz), _midnight(last_day + timedelta(days=1), tz), "date"
    else:
        start_utc, end_utc, kind = _aware(start, "start"), _aware(end, "end"), "timestamp"
        if end_utc <= start_utc:
            raise PeriodError("INVALID_PARAMETER", "end must be after start.")
        first_day = start_utc.astimezone(tz).date()
        last_day = (end_utc - timedelta(microseconds=1)).astimezone(tz).date()

    if (last_day - first_day).days + 1 > MAX_PERIOD_DAYS:
        raise PeriodError("INVALID_PARAMETER", f"The period may not exceed {MAX_PERIOD_DAYS} days.")
    return Period(tz.key, kind, start_utc, end_utc, first_day, last_day)


def build_period(start: str, end: str, timezone_name: str | None) -> Period:
    """Plain period (no after-hours windows): the shared D-03/D-10 parsing used by ``devices.offline_frequency``."""
    return _resolve_bounds(start, end, load_timezone(timezone_name))


def build_after_hours_period(start: str, end: str, after_local: str | None, timezone_name: str) -> AfterHoursPeriod:
    tz = load_timezone(timezone_name)
    after = parse_after_local(after_local)
    period = _resolve_bounds(start, end, tz)
    start_utc, end_utc, kind = period.start_utc, period.end_utc_exclusive, period.input_kind
    first_day, last_day = period.first_day, period.last_day

    after_is_midnight = after == time(0, 0)
    windows, day = [], first_day
    while day <= last_day:
        w_start = resolve_local(day, after, tz, midnight_rule=after_is_midnight)
        w_end = _midnight(day + timedelta(days=1), tz)
        w_start, w_end = max(w_start, start_utc), min(w_end, end_utc)
        if w_start < w_end:
            windows.append((w_start, w_end))
        day += timedelta(days=1)
    return AfterHoursPeriod(tz.key, kind, start_utc, end_utc, after, tuple(windows))
