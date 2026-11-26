"""doors.after_hours: physical door openings (``door_opened``) inside a building's after-hours windows.

Deterministic and read-only. All SQL is static text with bound parameters; the model never supplies SQL.

Counting rules (ADR-0001 D-01/D-04/D-12, plus the task decisions recorded in docs/DOORS_AFTER_HOURS.md)
* Only ``door_opened`` events of ACCESS_CONTROL assets in the requested building count as a physical opening.
* ``door_forced`` is never counted and never linked to ``door_opened``: the contract has no correlation identifier.
  When such events occur in the windows they are reported via ``CORRELATION_UNAVAILABLE`` (plus the per-door
  ``uncounted_door_forced_events``) and the result is PARTIAL, because the opening count may be an underestimate.
* Duplicate deliveries (same ``source_id`` + ``source_event_id``) collapse to one. Events without identity are counted
  individually and flagged ``MISSING_EVENT_IDENTITY`` (D-04).
* The result is COMPLETE only when a trusted coverage provider confirms ingestion, the building timezone was
  explicitly configured and no door_forced events fell in the windows; otherwise PARTIAL. Row-less results are never presented as a confirmed zero unless COMPLETE.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from app.domain.tools.coverage import CoverageProvider, assess_coverage
from app.domain.tools.envelope import (EnvelopeBuilder, Envelope, EvidenceRef, Limitation, LimitationCode,
                                       ResultStatus, Row, cannot_compute, unsupported)
from app.domain.tools.periods import PeriodError, build_after_hours_period
from app.domain.tools.spec import ToolSpec

TOOL_NAME = "doors.after_hours"
TOOL_VERSION = "1"
COLUMNS = ("building_code", "source_code", "door_external_id", "asset_code", "asset_name", "opening_count",
           "uncounted_door_forced_events")
OPENING_EVENT = "door_opened"
FORCED_EVENT = "door_forced"
# Migration 009 filled every NULL buildings.timezone with this value and left no marker, so the stored value alone
# cannot prove an operator chose it. Any other value cannot come from that backfill.
BACKFILL_DEFAULT_TIMEZONE = "Etc/GMT-2"

SQL_BUILDING = "SELECT building_id, building_code, timezone FROM buildings WHERE building_code = %(building_code)s"

SQL_DOOR_SOURCES = """
SELECT DISTINCT ds.source_code
  FROM data_sources ds
  JOIN asset_external_ids aei ON aei.source_id = ds.source_id
  JOIN assets a ON a.asset_id = aei.asset_id
  JOIN system_types st ON st.system_type_id = a.system_type_id
  JOIN locations l ON l.location_id = a.location_id
 WHERE ds.building_id = %(building_id)s AND l.building_id = %(building_id)s AND st.code = 'ACCESS_CONTROL'
 ORDER BY ds.source_code"""

SQL_EVENTS = """
WITH windows AS (
    SELECT w.ws, w.we
      FROM unnest(%(window_starts)s::timestamptz[], %(window_ends)s::timestamptz[]) AS w(ws, we)
)
SELECT er.event_id, er.ts, et.name AS event_type, er.source_id, ds.source_code, er.source_event_id,
       a.asset_id, a.asset_code, a.name AS asset_name,
       (SELECT array_agg(aei.external_id ORDER BY aei.external_id)
          FROM asset_external_ids aei
         WHERE aei.source_id = er.source_id AND aei.asset_id = er.asset_id) AS external_ids
  FROM event_records er
  JOIN event_types et ON et.event_type_id = er.event_type_id
  JOIN assets a ON a.asset_id = er.asset_id
  JOIN locations l ON l.location_id = a.location_id
  JOIN system_types st ON st.system_type_id = a.system_type_id
  LEFT JOIN data_sources ds ON ds.source_id = er.source_id
 WHERE l.building_id = %(building_id)s
   AND st.code = 'ACCESS_CONTROL'
   AND et.name IN (%(opening_event)s, %(forced_event)s)
   AND EXISTS (SELECT 1 FROM windows w WHERE er.ts >= w.ws AND er.ts < w.we)
 ORDER BY er.ts, er.event_id"""

PARAMETERS_SCHEMA = {
    "type": "object",
    "properties": {
        "building": {"type": "string", "description": "Building code, e.g. BLD-ALPHA."},
        "start": {"type": "string", "description": "Period start: a building-local date YYYY-MM-DD, or a timestamp "
                                                     "with an explicit UTC offset."},
        "end": {"type": "string", "description": "Period end, same form as start. A date is inclusive; a timestamp "
                                                   "is exclusive."},
        "after_local": {"type": "string", "description": "Local time after which a day counts as after-hours, "
                                                           "HH:MM. Defaults to 20:00. The window runs to local midnight."},
    },
    "required": ["building", "start", "end"],
}
DESCRIPTION = ("Count physical door openings (door_opened events) per door during after-hours windows "
               "(after_local until local midnight, in the building's timezone) over a period. Returns a result "
               "envelope with evidence per door and explicit completeness limitations. door_forced alarms are not "
               "counted as openings.")


@dataclass
class _Door:
    key: tuple
    building_code: str
    source_code: str | None
    external_ids: tuple[str, ...]
    asset_code: str
    asset_name: str
    refs: list[EvidenceRef] = field(default_factory=list)
    forced: int = 0


@dataclass(frozen=True)
class Aggregation:
    doors: tuple[_Door, ...]
    duplicates_collapsed: int
    missing_identity_openings: int
    forced_total: int


def aggregate_events(events: Sequence[Mapping[str, Any]], building_code: str) -> Aggregation:
    """Pure counting core: dedupe, keep ``door_opened`` only, group per (asset, source)."""
    doors: "OrderedDict[tuple, _Door]" = OrderedDict()
    seen: set[tuple] = set()
    duplicates = missing_identity = forced_total = 0
    for ev in events:
        etype = ev["event_type"]
        if etype not in (OPENING_EVENT, FORCED_EVENT):
            continue
        key = (ev["asset_id"], ev.get("source_id"))
        door = doors.get(key)
        if door is None:
            door = doors[key] = _Door(key, building_code, ev.get("source_code"),
                                      tuple(ev.get("external_ids") or ()), ev["asset_code"], ev["asset_name"])
        if etype == FORCED_EVENT:
            door.forced += 1
            forced_total += 1
            continue
        if ev.get("source_id") is not None and ev.get("source_event_id") is not None:
            identity = (ev["source_id"], ev["source_event_id"])
            if identity in seen:
                duplicates += 1
                continue
            seen.add(identity)
        else:
            missing_identity += 1
        door.refs.append(EvidenceRef("event_record", str(ev["event_id"]), ev["ts"], OPENING_EVENT))
    return Aggregation(tuple(doors.values()), duplicates, missing_identity, forced_total)


def _sort_key(door: _Door):
    return (-len(door.refs), door.building_code, door.source_code or "", ", ".join(door.external_ids), door.asset_code)


def _echo(building: str, start: str, end: str, after_local: str | None, period=None) -> dict[str, Any]:
    out: dict[str, Any] = {"building": building, "start": start, "end": end,
                           "after_local": after_local if after_local is not None else "20:00"}
    if period is not None:
        out.update({"timezone": period.timezone_name, "start_utc": period.start_utc,
                    "end_utc_exclusive": period.end_utc_exclusive, "window_count": len(period.windows)})
        if len(period.windows) <= 14:
            out["after_hours_windows_utc"] = [[a, b] for a, b in period.windows]
    return out


def run_doors_after_hours(cur, *, building: str, start: str, end: str, after_local: str | None = None,
                          coverage_provider: CoverageProvider | None = None,
                          confirmed_timezone_buildings: frozenset[str] = frozenset()) -> Envelope:
    """``cur`` must be a dict-row cursor in a read-only transaction; only SELECTs are issued."""
    echo = _echo(building, start, end, after_local)

    def fail(code: LimitationCode, message: str, details=None, period=None) -> Envelope:
        return cannot_compute(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=_echo(building, start, end, after_local, period),
                              columns=COLUMNS, limitation=Limitation(code, message, details or {}))

    cur.execute(SQL_BUILDING, {"building_code": building})
    found = cur.fetchone()
    if not found:
        return fail(LimitationCode.INVALID_PARAMETER, f"Unknown building code {building!r}.")
    try:
        period = build_after_hours_period(start, end, after_local, found["timezone"])
    except PeriodError as exc:
        return fail(LimitationCode(exc.code), str(exc))
    echo = _echo(building, start, end, after_local, period)

    cur.execute(SQL_DOOR_SOURCES, {"building_id": found["building_id"]})
    source_codes = [r["source_code"] for r in cur.fetchall()]
    coverage = assess_coverage(coverage_provider, found["building_code"], source_codes,
                               period.start_utc, period.end_utc_exclusive)
    if coverage.all_unsupported and source_codes:
        return unsupported(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=echo, columns=COLUMNS,
                           limitation=coverage.limitations[0])

    cur.execute(SQL_EVENTS, {"building_id": found["building_id"], "opening_event": OPENING_EVENT,
                             "forced_event": FORCED_EVENT,
                             "window_starts": [w[0] for w in period.windows],
                             "window_ends": [w[1] for w in period.windows]})
    agg = aggregate_events(cur.fetchall(), found["building_code"])

    limitations = list(coverage.limitations)
    status = ResultStatus.COMPLETE if coverage.confirmed_complete else ResultStatus.PARTIAL
    if period.timezone_name == BACKFILL_DEFAULT_TIMEZONE and building not in confirmed_timezone_buildings:
        status = ResultStatus.PARTIAL
        limitations.append(Limitation(
            LimitationCode.TIMEZONE_PROVENANCE_UNCONFIRMED,
            "The building timezone equals the value migration 009 backfilled automatically, and nothing records "
            "whether an operator set it. It is also a fixed UTC offset with no DST rules, so after-hours "
            "boundaries may be wrong around DST changes.",
            {"timezone": period.timezone_name}))
    if agg.forced_total:
        # A forced-open alarm may be the only record of a physical opening, and without a correlation identifier it
        # cannot be matched to a door_opened event, so the count may be an underestimate: never COMPLETE.
        status = ResultStatus.PARTIAL
        limitations.append(Limitation(
            LimitationCode.CORRELATION_UNAVAILABLE,
            "door_forced events occurred in the windows but were not counted as openings or linked to door_opened "
            "events (the ingestion contract has no correlation identifier), so the opening count may be an underestimate.",
            {"uncounted_door_forced_events": agg.forced_total}))
    if agg.missing_identity_openings:
        limitations.append(Limitation(
            LimitationCode.MISSING_EVENT_IDENTITY,
            "Some counted openings have no source event identity, so duplicate deliveries cannot be excluded.",
            {"openings_without_identity": agg.missing_identity_openings}))

    doors = sorted((d for d in agg.doors if d.refs), key=_sort_key)
    rows = [Row({"building_code": d.building_code, "source_code": d.source_code,
                 "door_external_id": ", ".join(d.external_ids) or None, "asset_code": d.asset_code,
                 "asset_name": d.asset_name, "opening_count": len(d.refs),
                 "uncounted_door_forced_events": d.forced}, tuple(d.refs)) for d in doors]
    return EnvelopeBuilder(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=echo, data_as_of=coverage.data_as_of,
                           source_ids=source_codes, columns=COLUMNS, rows=rows, limitations=limitations,
                           status=status).build()


def build_doors_after_hours_spec(cursor_factory: Callable[[], AbstractContextManager], *,
                                 coverage_provider: CoverageProvider | None = None,
                                 confirmed_timezone_buildings: frozenset[str] = frozenset()) -> ToolSpec:
    """``cursor_factory()`` is a context manager yielding a dict-row cursor in a READ ONLY transaction.

    ``coverage_provider`` and ``confirmed_timezone_buildings`` are trusted wiring, deliberately absent from the
    tool's parameters so callers cannot assert completeness.
    """
    def handler(**arguments: Any) -> Envelope:
        with cursor_factory() as cur:
            return run_doors_after_hours(cur, coverage_provider=coverage_provider,
                                         confirmed_timezone_buildings=confirmed_timezone_buildings, **arguments)
    return ToolSpec(TOOL_NAME, DESCRIPTION, PARAMETERS_SCHEMA, handler, TOOL_VERSION)
