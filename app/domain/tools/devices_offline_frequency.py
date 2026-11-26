"""devices.offline_frequency: how often, and for how long, devices were Offline in a period, as outage *sessions*.

Deterministic and read-only. All SQL is static text with bound parameters; the model never supplies SQL.

Semantics (ADR-0001 D-05..D-12; golden fixtures S-C..S-T in tests/fixtures/analytics; notes in
docs/DEVICES_OFFLINE_FREQUENCY.md)

* Evidence of an outage is an explicit availability transition and nothing else. Eligible event types come from the
  catalogue added by migration 014 (``outage_class = 'AVAILABILITY'``: camera_offline/camera_online, link_down/link_up).
  FAULT-class pairs (hvac_fault, fire_alarm_triggered...) are never evidence of Offline (D-05). SILENCE IS NOT AN
  OUTAGE: a device that stops sending data, or a source that goes quiet, produces no session (G-OFF-02).
* One state machine per (asset, source): UNKNOWN | ONLINE | OFFLINE (D-06).
    OFFLINE_START  UNKNOWN/ONLINE -> OFFLINE and opens a session; OFFLINE -> OFFLINE (repeated alarm, same session).
    OFFLINE_END    OFFLINE -> ONLINE and closes the session; otherwise an orphan: counted, never a session.
  Sessions are never merged because they are close in time (D-07); FLAPPING is reported as ``not_evaluated``.
* The state at period start comes from an unbounded look-back to the last reliable earlier transition (D-08, open
  question d). With none, the state is UNKNOWN: neither an outage nor its absence is assumed.
* Only the overlap with ``[start, end)`` is counted; a session that started earlier is ``ongoing_at_period_start``
  and is not counted as started in the period.
* A session without a recovery event stays open and no recovery time is invented (D-09). Its duration is measured to
  the earliest of the period end, the source's ``data_as_of`` (when a coverage provider reports it) and the query
  time, and is flagged as a lower bound.
* ``state_at_period_end`` (OFFLINE | ONLINE | UNKNOWN) is known only when trusted coverage is confirmed for the whole period
  and the cutoff is the period end itself; otherwise UNKNOWN (H1). ``open_at_period_end`` is kept for compatibility and
  means only "no recovery observed up to the cutoff".
* Duplicate deliveries (same ``source_id`` + ``source_event_id``) collapse to one; events without identity are kept
  individually and flagged ``MISSING_EVENT_IDENTITY`` (D-04).
* COMPLETE only when a trusted coverage provider confirms the whole period, the building timezone is explicit and
  every requested system is eligible; otherwise PARTIAL (or UNSUPPORTED / CANNOT_COMPUTE).
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from app.domain.tools.coverage import CoverageProvider, assess_coverage
from app.domain.tools.doors_after_hours import BACKFILL_DEFAULT_TIMEZONE
from app.domain.tools.envelope import (EnvelopeBuilder, Envelope, EvidenceRef, Limitation, LimitationCode,
                                       ResultStatus, Row, cannot_compute, unsupported)
from app.domain.tools.periods import Period, PeriodError, build_period
from app.domain.tools.spec import ToolSpec

TOOL_NAME = "devices.offline_frequency"
TOOL_VERSION = "1"
COLUMNS = ("building_code", "source_code", "asset_external_id", "asset_code", "asset_name", "system_type",
           "initial_state", "outages_started_in_period", "outages_ongoing_at_period_start",
           "downtime_seconds_in_period", "open_at_period_end", "state_at_period_end", "duration_is_lower_bound", "flapping",
           "orphan_offline_end_count", "orphan_offline_end_refs", "duplicate_events_ignored",
           "initial_state_evidence_refs", "sessions")
EVIDENCE_KIND = "event_record"
OPEN_CLASS, CLOSE_CLASS = "FAULT_OPEN", "FAULT_CLOSE"        # event_types.event_class (migration 014)
ONLINE, OFFLINE, UNKNOWN = "ONLINE", "OFFLINE", "UNKNOWN"
FLAPPING_NOT_EVALUATED = "not_evaluated"                      # D-07: no window is configured, none is invented
RANK_STARTED, RANK_DOWNTIME = "outages_started", "downtime_seconds"
RANK_CHOICES = (RANK_STARTED, RANK_DOWNTIME)

# ---------------------------------------------------------------------------------------------------- SQL
SQL_BUILDING = "SELECT building_id, building_code, timezone FROM buildings WHERE building_code = %(building_code)s"

SQL_CATALOGUE_READY = """
SELECT count(*) AS n
  FROM information_schema.columns
 WHERE table_schema = current_schema() AND table_name = 'event_types'
   AND column_name IN ('event_class', 'outage_class', 'resolves_event_type_id')"""

# A system type is eligible when the catalogue says some AVAILABILITY event type is valid for it (D-05, open question h).
SQL_SYSTEMS = """
SELECT st.code,
       EXISTS (SELECT 1
                 FROM event_type_system_compat c
                 JOIN event_types et ON et.event_type_id = c.event_type_id
                WHERE c.system_type_id = st.system_type_id
                  AND et.outage_class = 'AVAILABILITY'
                  AND et.event_class IN ('FAULT_OPEN', 'FAULT_CLOSE')) AS eligible
  FROM system_types st
 ORDER BY st.code"""

SQL_SOURCES = """
SELECT DISTINCT ds.source_code
  FROM data_sources ds
  JOIN asset_external_ids aei ON aei.source_id = ds.source_id
  JOIN assets a ON a.asset_id = aei.asset_id
  JOIN system_types st ON st.system_type_id = a.system_type_id
  JOIN locations l ON l.location_id = a.location_id
 WHERE ds.building_id = %(building_id)s AND l.building_id = %(building_id)s
   AND st.code = ANY(%(systems)s::text[])
   AND (%(source)s::text IS NULL OR ds.source_code = %(source)s::text)
 ORDER BY ds.source_code"""

_COLS = """event_id, ts, event_type, event_class, source_id, source_code, source_event_id, asset_id, asset_code,
       asset_name, system_code, external_ids"""


def _avail_cte(ts_predicate: str) -> str:
    """Availability transitions (migration 014 catalogue) of the building's in-scope assets. Static text."""
    return f"""
WITH avail AS (
    SELECT er.event_id, er.ts, et.name AS event_type, et.event_class, er.source_id, ds.source_code,
           er.source_event_id, a.asset_id, a.asset_code, a.name AS asset_name, st.code AS system_code,
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
       AND et.outage_class = 'AVAILABILITY'
       AND et.event_class IN ('FAULT_OPEN', 'FAULT_CLOSE')
       AND st.code = ANY(%(systems)s::text[])
       AND (%(source)s::text IS NULL OR ds.source_code = %(source)s::text)
       AND {ts_predicate}
)"""


SQL_PERIOD_EVENTS = _avail_cte("er.ts >= %(start_utc)s::timestamptz AND er.ts < %(end_utc)s::timestamptz") + f"""
SELECT {_COLS}
  FROM avail
 ORDER BY ts, event_id"""

# Unbounded look-back, bounded in volume: per (asset, source) only the last recovery before the period (the anchor),
# the first start after it (where the open session began) and the last earlier event (the state at period start).
SQL_LOOKBACK_EVENTS = _avail_cte("er.ts < %(start_utc)s::timestamptz") + f""",
anchor AS (
    SELECT DISTINCT ON (asset_id, source_id) asset_id, source_id, ts AS anchor_ts, event_id AS anchor_id
      FROM avail
     WHERE event_class = 'FAULT_CLOSE'
     ORDER BY asset_id, source_id, ts DESC, event_id DESC
),
first_open AS (
    SELECT DISTINCT ON (a.asset_id, a.source_id) a.event_id
      FROM avail a
      LEFT JOIN anchor n ON n.asset_id = a.asset_id AND n.source_id IS NOT DISTINCT FROM a.source_id
     WHERE a.event_class = 'FAULT_OPEN'
       AND (n.anchor_id IS NULL OR (a.ts, a.event_id) > (n.anchor_ts, n.anchor_id))
     ORDER BY a.asset_id, a.source_id, a.ts, a.event_id
),
last_event AS (
    SELECT DISTINCT ON (asset_id, source_id) event_id
      FROM avail
     ORDER BY asset_id, source_id, ts DESC, event_id DESC
)
SELECT {_COLS}
  FROM avail
 WHERE event_id IN (SELECT anchor_id FROM anchor)
    OR event_id IN (SELECT event_id FROM first_open)
    OR event_id IN (SELECT event_id FROM last_event)
 ORDER BY ts, event_id"""

PARAMETERS_SCHEMA = {
    "type": "object",
    "properties": {
        "building": {"type": "string", "description": "Building code, e.g. BLD-ALPHA."},
        "start": {"type": "string", "description": "Period start: a building-local date YYYY-MM-DD, or a timestamp "
                                                     "with an explicit UTC offset."},
        "end": {"type": "string", "description": "Period end, same form as start. A date is inclusive; a timestamp "
                                                   "is exclusive."},
        "systems": {"type": "array", "items": {"type": "string"},
                    "description": "Optional system type codes to include, e.g. [\"CCTV\", \"NETWORKING\"]. Only systems "
                                   "whose sources report explicit offline/online events are supported; default: all of them."},
        "source": {"type": "string", "description": "Optional source code to restrict the analysis to one data source."},
        "rank_by": {"type": "string", "description": "Row order: outages_started (default) or downtime_seconds."},
    },
    "required": ["building", "start", "end"],
}
DESCRIPTION = ("Offline frequency per device over a period: groups explicit offline/online events (camera_offline/"
               "camera_online, link_down/link_up) into outage sessions, merging repeated alarms of the same outage, and "
               "returns per device the number of sessions, each session's duration and the total downtime inside the "
               "period. Outages still open at the end of the period are reported as open with a lower-bound duration. "
               "Silence, missing data or fault/alarm events are never treated as an outage. Returns a result envelope "
               "with evidence per session and explicit completeness limitations.")


# ---------------------------------------------------------------------------------------------------- core
@dataclass
class _Session:
    start: datetime
    refs: list[EvidenceRef] = field(default_factory=list)
    openers: int = 0
    end: datetime | None = None


@dataclass
class DeviceAnalysis:
    """Result of replaying one (asset, source)'s transitions against a period. Pure data, no formatting."""
    key: tuple
    source_code: str | None
    external_ids: tuple[str, ...]
    asset_code: str
    asset_name: str
    system_type: str | None
    initial_state: str
    sessions: list[_Session]
    orphans: list[EvidenceRef]
    initial_refs: list[EvidenceRef]
    all_refs: list[EvidenceRef]
    in_period_events: int
    duplicates: int
    missing_identity: int
    final_state: str = UNKNOWN          # state after replaying every event this device has (before any coverage check)


def _ref(ev: Mapping[str, Any]) -> EvidenceRef:
    return EvidenceRef(EVIDENCE_KIND, str(ev["event_id"]), ev["ts"], ev["event_type"])


def analyse_events(events: Sequence[Mapping[str, Any]], period_start: datetime) -> list[DeviceAnalysis]:
    """Pure state-machine core. ``events`` may mix look-back events (ts < period_start) and in-period events.

    Per (asset, source): collapse duplicate deliveries, order by (ts, event_id), replay look-back events to get the
    initial state, then replay the period. Unknown ``event_class`` values are ignored (never guessed).
    """
    groups: "OrderedDict[tuple, list[Mapping[str, Any]]]" = OrderedDict()
    for ev in sorted(events, key=lambda e: (e["ts"], e["event_id"])):
        if ev["event_class"] in (OPEN_CLASS, CLOSE_CLASS):
            groups.setdefault((ev["asset_id"], ev.get("source_id")), []).append(ev)

    out: list[DeviceAnalysis] = []
    for key, evs in groups.items():
        seen: set[tuple] = set()
        kept: list[Mapping[str, Any]] = []
        duplicates = missing = 0
        for ev in evs:
            if ev.get("source_id") is not None and ev.get("source_event_id") is not None:
                identity = (ev["source_id"], ev["source_event_id"])
                if identity in seen:
                    duplicates += 1
                    continue
                seen.add(identity)
            else:
                missing += 1
            kept.append(ev)

        state, current = UNKNOWN, None
        sessions: list[_Session] = []
        orphans: list[EvidenceRef] = []
        initial_state: str | None = None
        initial_refs: list[EvidenceRef] = []
        in_period = 0
        for ev in kept:
            in_this_period = ev["ts"] >= period_start
            if in_this_period and initial_state is None:
                initial_state = state
            ref = _ref(ev)
            if not in_this_period:
                initial_refs.append(ref)
            else:
                in_period += 1
            if ev["event_class"] == OPEN_CLASS:
                if state == OFFLINE:
                    current.refs.append(ref)          # repeated alarm inside the ongoing outage: same session
                    current.openers += 1
                else:
                    current = _Session(ev["ts"], [ref], 1)
                    sessions.append(current)
                    state = OFFLINE
            else:  # CLOSE_CLASS
                if state == OFFLINE:
                    current.refs.append(ref)
                    current.end = ev["ts"]
                    current, state = None, ONLINE
                else:
                    if in_this_period:
                        orphans.append(ref)           # D-06: recorded, creates no session
                    state = ONLINE                    # the last reliable transition says "recovered"
        if initial_state is None:
            initial_state = state
        first = kept[0]
        out.append(DeviceAnalysis(key, first.get("source_code"), tuple(first.get("external_ids") or ()),
                                  first["asset_code"], first["asset_name"], first.get("system_code"), initial_state,
                                  sessions, orphans, initial_refs, [_ref(e) for e in kept], in_period, duplicates,
                                  missing, state))
    return out


def _secs(delta: timedelta) -> int | float:
    return int(delta.total_seconds()) if delta.microseconds == 0 else round(delta.total_seconds(), 6)


def _cutoff(period_end: datetime, data_as_of: datetime | None, now: datetime) -> tuple[datetime, str]:
    """D-09: the earlier of period end and data_as_of. The query time is a further, trusted upper bound so an
    outage is never extended into the future when the requested period has not ended yet."""
    best, reason = period_end, "period_end"
    if data_as_of is not None and data_as_of < best:
        best, reason = data_as_of, "data_as_of"
    if now < best:
        best, reason = now, "query_time"
    return best, reason


def render_sessions(dev: DeviceAnalysis, period_start: datetime, period_end: datetime,
                    data_as_of: datetime | None, now: datetime) -> list[dict[str, Any]]:
    cutoff, reason = _cutoff(period_end, data_as_of, now)
    out = []
    for s in dev.sessions:
        if s.end is not None and s.end <= period_start and s.start < period_start:
            continue                                   # ended before the period began: no overlap, not started here
        is_open = s.end is None
        o_start = max(s.start, period_start)
        o_end = cutoff if is_open else min(s.end, period_end)
        overlap = max(timedelta(0), o_end - o_start)
        out.append({
            "start_utc": s.start, "end_utc": s.end, "open": is_open,
            "started_in_period": s.start >= period_start,
            "ongoing_at_period_start": s.start < period_start,
            "overlap_start_utc": o_start, "overlap_end_utc": max(o_start, o_end), "overlap_seconds": _secs(overlap),
            "duration_seconds": None if is_open else _secs(s.end - s.start),
            "duration_is_lower_bound": is_open, "recovery_observed": not is_open,
            "cutoff_utc": cutoff if is_open else None, "cutoff_reason": reason if is_open else None,
            "repeated_start_events": max(0, s.openers - 1),
            "evidence_refs": [r.to_dict() for r in s.refs],
        })
    return out


def state_at_period_end(dev: DeviceAnalysis, coverage_reaches_period_end: bool) -> str:
    """ADR D-09 / H1: the device state *at the requested period end*.

    It is known only when trusted coverage reaches that boundary. Otherwise it is UNKNOWN: a missing later
    ``camera_online`` / ``link_up`` (or a later ``camera_offline``) cannot be told apart from data not yet retrieved.
    ``open_at_period_end`` only says "no recovery was observed up to the cutoff"; it is NOT a claim about this state.
    """
    return dev.final_state if coverage_reaches_period_end else UNKNOWN


def _row(dev: DeviceAnalysis, building_code: str, sessions: list[dict[str, Any]],
         coverage_reaches_period_end: bool = False) -> Row:
    started = sum(1 for s in sessions if s["started_in_period"])
    ongoing = sum(1 for s in sessions if s["ongoing_at_period_start"])
    any_open = any(s["open"] for s in sessions)
    downtime = sum(s["overlap_seconds"] for s in sessions)
    values = {
        "building_code": building_code, "source_code": dev.source_code,
        "asset_external_id": ", ".join(dev.external_ids) or None, "asset_code": dev.asset_code,
        "asset_name": dev.asset_name, "system_type": dev.system_type, "initial_state": dev.initial_state,
        "outages_started_in_period": started, "outages_ongoing_at_period_start": ongoing,
        "downtime_seconds_in_period": downtime, "open_at_period_end": any_open,
        "state_at_period_end": state_at_period_end(dev, coverage_reaches_period_end),
        "duration_is_lower_bound": any_open,
        "flapping": FLAPPING_NOT_EVALUATED, "orphan_offline_end_count": len(dev.orphans),
        "orphan_offline_end_refs": [r.to_dict() for r in dev.orphans], "duplicate_events_ignored": dev.duplicates,
        "initial_state_evidence_refs": [r.to_dict() for r in dev.initial_refs], "sessions": sessions,
    }
    return Row(values, tuple(dev.all_refs))


def _sort_key(rank_by: str):
    def key(row: Row):
        v = row.values
        primary = -v["outages_started_in_period"] if rank_by == RANK_STARTED else -v["downtime_seconds_in_period"]
        return (primary, v["building_code"], v["source_code"] or "", v["asset_external_id"] or "", v["asset_code"])
    return key


# ---------------------------------------------------------------------------------------------------- tool
def _safe(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    return str(value)


def _echo(building, start, end, systems, source, rank_by, period: Period | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"building": _safe(building), "start": _safe(start), "end": _safe(end),
                           "systems": _safe(systems), "source": _safe(source),
                           "rank_by": _safe(rank_by) if rank_by is not None else RANK_STARTED}
    if period is not None:
        out.update({"timezone": period.timezone_name, "start_utc": period.start_utc,
                    "end_utc_exclusive": period.end_utc_exclusive})
    return out


def run_devices_offline_frequency(cur, *, building: str, start: str, end: str, systems: Sequence[str] | None = None,
                                  source: str | None = None, rank_by: str | None = None,
                                  coverage_provider: CoverageProvider | None = None,
                                  confirmed_timezone_buildings: frozenset[str] = frozenset(),
                                  max_rows: int | None = None,
                                  clock: Callable[[], datetime] | None = None) -> Envelope:
    """``cur`` must be a dict-row cursor in a read-only transaction; only SELECTs are issued."""
    clock = clock or (lambda: datetime.now(timezone.utc))

    def fail(code: LimitationCode, message: str, details=None, period=None, systems_echo=systems) -> Envelope:
        return cannot_compute(tool=TOOL_NAME, query_version=TOOL_VERSION,
                              parameters=_echo(building, start, end, systems_echo, source, rank_by, period),
                              columns=COLUMNS, limitation=Limitation(code, message, details or {}), clock=clock)

    # -- parameter validation (structural checks already happened in ToolSpec.validate_arguments)
    if systems is not None and (isinstance(systems, (str, bytes)) or not all(isinstance(s, str) and s.strip() for s in systems)):
        return fail(LimitationCode.INVALID_PARAMETER, "systems must be a list of system type codes.")
    if source is not None and not (isinstance(source, str) and source.strip()):
        return fail(LimitationCode.INVALID_PARAMETER, "source must be a non-empty source code.")
    if rank_by is not None and rank_by not in RANK_CHOICES:
        return fail(LimitationCode.INVALID_PARAMETER, f"rank_by must be one of {list(RANK_CHOICES)}.")
    requested = sorted({s.strip().upper() for s in systems}) if systems is not None else None
    source = source.strip() if source is not None else None
    rank_by = rank_by or RANK_STARTED

    cur.execute(SQL_BUILDING, {"building_code": building})
    found = cur.fetchone()
    if not found:
        return fail(LimitationCode.INVALID_PARAMETER, f"Unknown building code {building!r}.", systems_echo=requested)
    try:
        period = build_period(start, end, found["timezone"])
    except PeriodError as exc:
        return fail(LimitationCode(exc.code), str(exc), systems_echo=requested)
    echo = _echo(building, start, end, requested, source, rank_by, period)

    def fail_p(code, message, details=None):
        return fail(code, message, details, period, requested)

    cur.execute(SQL_CATALOGUE_READY)
    if cur.fetchone()["n"] != 3:
        return fail_p(LimitationCode.CATALOGUE_NOT_MIGRATED,
                      "The event catalogue has no event_class / outage_class / resolves_event_type_id columns "
                      "(migration 014_event_catalogue_pairing); availability transitions cannot be identified.")

    # -- which systems can be analysed (D-05, data-driven from the catalogue)
    cur.execute(SQL_SYSTEMS)
    system_rows = cur.fetchall()
    known = {r["code"] for r in system_rows}
    eligible = {r["code"] for r in system_rows if r["eligible"]}
    limitations: list[Limitation] = []
    force_partial = False
    if requested is None:
        scope = sorted(eligible)
    else:
        unknown = [s for s in requested if s not in known]
        if unknown:
            return fail_p(LimitationCode.INVALID_PARAMETER, f"Unknown system type code(s): {unknown}.",
                          {"known_systems": sorted(known)})
        scope = [s for s in requested if s in eligible]
        not_eligible = [s for s in requested if s not in eligible]
        if not_eligible:
            lim = Limitation(LimitationCode.SYSTEM_NOT_ELIGIBLE_FOR_OFFLINE,
                             "These system types have no explicit offline/online transitions in the event catalogue, "
                             "so no outage can be derived for them. They are absent from the result, which is not "
                             "the same as zero outages.",
                             {"unsupported_systems": not_eligible, "eligible_systems": sorted(eligible)})
            if not scope:
                return unsupported(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=echo, columns=COLUMNS,
                                   limitation=lim, clock=clock)
            limitations.append(lim)
            force_partial = True
    if not scope:
        return unsupported(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=echo, columns=COLUMNS, clock=clock,
                           limitation=Limitation(LimitationCode.SYSTEM_NOT_ELIGIBLE_FOR_OFFLINE,
                                                 "No system type has availability (offline/online) event types in the catalogue.",
                                                 {"eligible_systems": []}))

    binds = {"building_id": found["building_id"], "systems": scope, "source": source}
    cur.execute(SQL_SOURCES, binds)
    source_codes = [r["source_code"] for r in cur.fetchall()]
    coverage = assess_coverage(coverage_provider, found["building_code"], source_codes, period.start_utc,
                               period.end_utc_exclusive, source_label="device")
    if coverage.all_unsupported and source_codes:
        return unsupported(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=echo, columns=COLUMNS,
                           limitation=coverage.limitations[0], clock=clock)

    window = {"start_utc": period.start_utc, "end_utc": period.end_utc_exclusive}
    cur.execute(SQL_LOOKBACK_EVENTS, {**binds, **window})
    events = list(cur.fetchall())
    cur.execute(SQL_PERIOD_EVENTS, {**binds, **window})
    events += list(cur.fetchall())
    devices = analyse_events(events, period.start_utc)

    limitations = list(coverage.limitations) + limitations
    status = ResultStatus.COMPLETE if coverage.confirmed_complete and not force_partial else ResultStatus.PARTIAL
    if period.timezone_name == BACKFILL_DEFAULT_TIMEZONE and building not in confirmed_timezone_buildings:
        status = ResultStatus.PARTIAL
        limitations.append(Limitation(
            LimitationCode.TIMEZONE_PROVENANCE_UNCONFIRMED,
            "The building timezone equals the value migration 009 backfilled automatically, and nothing records "
            "whether an operator set it. It is also a fixed UTC offset with no DST rules, so period boundaries "
            "may be wrong around DST changes.",
            {"timezone": period.timezone_name}))

    now = clock()
    paired: list[tuple[Row, DeviceAnalysis]] = []
    for dev in devices:
        if dev.in_period_events == 0 and dev.initial_state != OFFLINE:
            continue          # no evidence inside the period and not known to be down at its start: no row (not a zero)
        source_as_of = coverage.source_data_as_of.get(dev.source_code)
        sessions = render_sessions(dev, period.start_utc, period.end_utc_exclusive, source_as_of, now)
        # H1: coverage must be confirmed for the whole period AND the cutoff must be the period end itself
        # (not data_as_of, not the query time) before anything is claimed about the state at the period end.
        reaches_end = coverage.confirmed_complete and _cutoff(period.end_utc_exclusive, source_as_of, now)[1] == "period_end"
        paired.append((_row(dev, found["building_code"], sessions, reaches_end), dev))
    paired.sort(key=lambda pair: _sort_key(rank_by)(pair[0]))
    emitted = [d for _, d in paired]
    row_objs = [r for r, _ in paired]

    if any(r.values["open_at_period_end"] for r in row_objs):
        open_n = sum(1 for r in row_objs for s in r.values["sessions"] if s["open"])
        limitations.append(Limitation(
            LimitationCode.OPEN_OUTAGE_LOWER_BOUND,
            "Some outages have no observed recovery event. They stay open (no recovery time is invented, the device "
            "is not claimed to be back online) and their duration is measured only up to the earliest of the period "
            "end, the source's data_as_of and the query time, so it is a lower bound.",
            {"open_sessions": open_n, "devices": sum(1 for r in row_objs if r.values["open_at_period_end"])}))
    unknown_n = sum(1 for d in emitted if d.initial_state == UNKNOWN)
    if unknown_n:
        limitations.append(Limitation(
            LimitationCode.INITIAL_STATE_UNKNOWN,
            "No earlier state was available; the device state at period start is UNKNOWN, so outages in progress "
            "at that time may be missing.", {"devices": unknown_n}))
    orphan_n = sum(len(d.orphans) for d in emitted)
    if orphan_n:
        limitations.append(Limitation(
            LimitationCode.ORPHAN_OFFLINE_END,
            "Some recovery events have no known preceding start. They were recorded as incomplete transitions and "
            "created no outage session.",
            {"orphan_offline_end_events": orphan_n, "devices": sum(1 for d in emitted if d.orphans)}))
    missing_n = sum(d.missing_identity for d in emitted)
    if missing_n:
        limitations.append(Limitation(
            LimitationCode.MISSING_EVENT_IDENTITY,
            "Some events have no source event identity, so duplicate deliveries cannot be excluded. Each is kept "
            "as its own event and none is merged by similarity.", {"events_without_identity": missing_n}))
    if row_objs:
        limitations.append(Limitation(
            LimitationCode.FLAPPING_NOT_EVALUATED,
            "No flapping window is configured, so flapping is not evaluated. Every confirmed offline-online-offline "
            "cycle is a separate session; sessions are never merged because they are close in time."))

    return EnvelopeBuilder(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=echo, data_as_of=coverage.data_as_of,
                           source_ids=source_codes, columns=COLUMNS, rows=row_objs, limitations=limitations,
                           status=status, max_rows=max_rows, clock=clock).build()


def build_devices_offline_frequency_spec(cursor_factory: Callable[[], AbstractContextManager], *,
                                         coverage_provider: CoverageProvider | None = None,
                                         confirmed_timezone_buildings: frozenset[str] = frozenset(),
                                         max_rows: int | None = None,
                                         clock: Callable[[], datetime] | None = None) -> ToolSpec:
    """``cursor_factory()`` is a context manager yielding a dict-row cursor in a READ ONLY transaction.

    ``coverage_provider``, ``confirmed_timezone_buildings`` and ``max_rows`` are trusted wiring, deliberately absent
    from the tool's parameters so callers (and the model) cannot assert completeness or lift the row cap.
    """
    def handler(**arguments: Any) -> Envelope:
        with cursor_factory() as cur:
            return run_devices_offline_frequency(cur, coverage_provider=coverage_provider,
                                                 confirmed_timezone_buildings=confirmed_timezone_buildings,
                                                 max_rows=max_rows, clock=clock, **arguments)
    return ToolSpec(TOOL_NAME, DESCRIPTION, PARAMETERS_SCHEMA, handler, TOOL_VERSION)
