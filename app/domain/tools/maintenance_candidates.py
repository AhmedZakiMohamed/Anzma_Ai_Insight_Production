"""maintenance.candidates: provisional, deterministic inspection prioritization.

The tool never predicts failure. It ranks canonical evidence using a versioned, fixed policy. A score is withheld
unless a trusted server-side coverage provider confirms both event and telemetry history for the whole window.
Downtime sessions are delegated to ``devices.offline_frequency``; this module contains no outage state machine.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Callable, Mapping, Sequence

from app.domain.alerts.alert_config import T
from app.domain.tools.coverage import SourceCoverage
from app.domain.tools.devices_offline_frequency import run_devices_offline_frequency
from app.domain.tools.doors_after_hours import BACKFILL_DEFAULT_TIMEZONE
from app.domain.tools.envelope import (Envelope, EnvelopeBuilder, EvidenceRef, Limitation, LimitationCode,
                                       ResultStatus, Row, cannot_compute)
from app.domain.tools.periods import Period, PeriodError, build_period
from app.domain.tools.spec import ToolSpec
from app.domain.evidence.numeric_integrity import (COUNT, DURATION_S, NUMBER, MetricSpec,
                                                   register_metric_specs)

TOOL_NAME = "maintenance.candidates"
TOOL_VERSION = "1"
POLICY_VERSION = "maintenance-priority-proposal-1"
MAX_WINDOW_DAYS = 90
MAX_FAULT_EVENTS = 20_000
MAX_TELEMETRY_ASSETS = 2_000
HIGH_THRESHOLD = 70.0
MEDIUM_THRESHOLD = 40.0
FAULT_PER_EVENT = 25.0
OUTAGE_SATURATION_SECONDS = 4 * 60 * 60


@dataclass(frozen=True)
class ScoringPolicy:
    """Server-owned immutable policy; never included in model-facing parameters."""
    version: str = POLICY_VERSION
    high_threshold: float = HIGH_THRESHOLD
    medium_threshold: float = MEDIUM_THRESHOLD
    availability_weights: tuple[float, float, float] = (0.40, 0.35, 0.25)
    fault_condition_weights: tuple[float, float] = (0.60, 0.40)

    def __post_init__(self):
        if not self.version.strip():
            raise ValueError("scoring policy version is required")
        if self.medium_threshold < 0 or self.high_threshold <= self.medium_threshold:
            raise ValueError("priority thresholds must be non-negative and high must exceed medium")
        for weights in (self.availability_weights, self.fault_condition_weights):
            if any(weight < 0 for weight in weights) or not math.isclose(sum(weights), 1.0):
                raise ValueError("scoring weights must be non-negative and sum to 1")


DEFAULT_SCORING_POLICY = ScoringPolicy()

# Source-code for the small set of measurements with existing, executable warning/critical thresholds.
# Units must exactly match the alert engine's canonical unit; there is intentionally no conversion here.
CONDITION_METRICS = {
    "HVAC": {"temperature": ("C", "hvac_temp_warn", "hvac_temp_crit")},
    "NETWORKING": {
        "bandwidth_util": ("%", "bandwidth_warn_pct", "bandwidth_crit_mean_pct"),
        "packet_loss": ("%", "packet_loss_warn", "packet_loss_crit_mean"),
    },
}

COLUMNS = (
    "building_code", "asset_code", "asset_name", "system_type", "source_code", "source_codes", "scoring_profile",
    "priority", "score", "rank_within_profile", "policy_version", "fault_count", "fault_score",
    "fault_missing_identity_count", "fault_identity_complete", "downtime_seconds",
    "downtime_score", "condition_metric", "condition_value", "condition_unit", "condition_score",
    "condition_quality", "data_sufficiency", "maintenance_context", "reasons",
)
register_metric_specs(TOOL_NAME, {
    "rank_within_profile": MetricSpec(COUNT), "fault_count": MetricSpec(COUNT),
    "fault_score": MetricSpec(NUMBER), "score": MetricSpec(NUMBER), "downtime_seconds": MetricSpec(DURATION_S),
    "downtime_score": MetricSpec(NUMBER), "condition_value": MetricSpec(NUMBER),
    "condition_score": MetricSpec(NUMBER),
})

PARAMETERS_SCHEMA = {
    "type": "object",
    "properties": {
        "building": {"type": "string", "description": "Building code."},
        "start": {"type": "string", "description": "Start date or timestamp with explicit UTC offset."},
        "end": {"type": "string", "description": "Inclusive end date or exclusive timestamp."},
        "systems": {"type": "array", "items": {"type": "string"}, "description": "Optional system codes."},
        "source": {"type": "string", "description": "Optional source code."},
    },
    "required": ["building", "start", "end"],
}
DESCRIPTION = (
    "Rank canonical assets for inspection using explicit fault recurrence, supported condition-threshold evidence, "
    "and (only for catalogue-eligible systems) outage sessions from devices.offline_frequency. A score is withheld "
    "unless trusted server-side event and telemetry history coverage is confirmed for the requested period. "
    "This is an inspection-priority aid, not a diagnosis or failure probability."
)

SQL_BUILDING = "SELECT building_id, building_code, timezone FROM buildings WHERE building_code = %(building)s"
SQL_SYSTEMS = "SELECT code FROM system_types ORDER BY code"
SQL_AVAILABILITY_SYSTEMS = """
SELECT DISTINCT st.code
  FROM system_types st
  JOIN event_type_system_compat c USING (system_type_id)
  JOIN event_types et USING (event_type_id)
 WHERE et.outage_class = 'AVAILABILITY' AND et.event_class IN ('FAULT_OPEN','FAULT_CLOSE')
 ORDER BY st.code"""
SQL_CATALOGUE_READY = """
SELECT count(*) AS n FROM information_schema.columns
 WHERE table_schema=current_schema() AND table_name='event_types'
   AND column_name IN ('event_class','outage_class','resolves_event_type_id')"""
SQL_SOURCES = """
SELECT DISTINCT ds.source_code
  FROM data_sources ds
  JOIN (SELECT source_id, asset_id FROM telemetry_records WHERE source_id IS NOT NULL
          AND ts >= %(start_utc)s AND ts < %(end_utc)s
        UNION SELECT source_id, asset_id FROM event_records WHERE source_id IS NOT NULL
          AND ts >= %(start_utc)s AND ts < %(end_utc)s) f USING (source_id)
  JOIN assets a USING (asset_id) JOIN locations l USING (location_id)
 WHERE ds.building_id = %(building_id)s AND l.building_id = ds.building_id
   AND (%(source)s::text IS NULL OR ds.source_code = %(source)s)
 ORDER BY ds.source_code"""
SQL_FAULTS = f"""
SELECT er.event_id, er.asset_id, er.ts, er.source_id, er.source_event_id, ds.source_code,
       et.name AS event_type, a.asset_code, a.name AS asset_name, st.code AS system_type
  FROM event_records er
  JOIN event_types et USING (event_type_id)
  JOIN assets a USING (asset_id)
  JOIN system_types st USING (system_type_id)
  JOIN locations l USING (location_id)
  JOIN buildings b USING (building_id)
  LEFT JOIN data_sources ds USING (source_id)
 WHERE b.building_id = %(building_id)s
   AND er.ts >= %(start_utc)s AND er.ts < %(end_utc)s
   AND et.event_class = 'FAULT_OPEN' AND et.outage_class = 'FAULT'
   AND (%(systems)s::text[] IS NULL OR st.code = ANY(%(systems)s))
   AND (%(source)s::text IS NULL OR ds.source_code = %(source)s)
 ORDER BY er.ts, er.event_id
 LIMIT {MAX_FAULT_EVENTS + 1}"""
CONDITION_NORMALIZATION_SQL = """CASE WHEN tr.value <= CASE WHEN st.code='HVAC' THEN
                                      CASE WHEN l.name='Server Room' THEN %(server_warn)s ELSE %(hvac_warn)s END
                                 WHEN mt.name='bandwidth_util' THEN %(bandwidth_warn)s ELSE %(loss_warn)s END THEN 0.0
                                ELSE LEAST(100.0, 50.0 + 50.0 * (tr.value - CASE WHEN st.code='HVAC' THEN
                                      CASE WHEN l.name='Server Room' THEN %(server_warn)s ELSE %(hvac_warn)s END
                                 WHEN mt.name='bandwidth_util' THEN %(bandwidth_warn)s ELSE %(loss_warn)s END)
                      / NULLIF((CASE WHEN st.code='HVAC' THEN
                                      CASE WHEN l.name='Server Room' THEN %(server_crit)s ELSE %(hvac_crit)s END
                                 WHEN mt.name='bandwidth_util' THEN %(bandwidth_crit)s ELSE %(loss_crit)s END)
                             - (CASE WHEN st.code='HVAC' THEN
                                      CASE WHEN l.name='Server Room' THEN %(server_warn)s ELSE %(hvac_warn)s END
                                 WHEN mt.name='bandwidth_util' THEN %(bandwidth_warn)s ELSE %(loss_warn)s END), 0)) END"""
SQL_CONDITION = f"""
SELECT DISTINCT ON (a.asset_id) tr.telemetry_id, tr.asset_id, tr.ts, tr.value, tr.quality,
       mt.name AS metric, mt.unit, ds.source_code, a.asset_code, a.name AS asset_name,
       st.code AS system_type, b.building_code,
       {CONDITION_NORMALIZATION_SQL} AS normalized_score,
       CASE WHEN st.code = 'HVAC' THEN
             CASE WHEN l.name = 'Server Room' THEN %(server_warn)s ELSE %(hvac_warn)s END
            WHEN mt.name = 'bandwidth_util' THEN %(bandwidth_warn)s ELSE %(loss_warn)s END AS warning_value,
       CASE WHEN st.code = 'HVAC' THEN
             CASE WHEN l.name = 'Server Room' THEN %(server_crit)s ELSE %(hvac_crit)s END
            WHEN mt.name = 'bandwidth_util' THEN %(bandwidth_crit)s ELSE %(loss_crit)s END AS critical_value
  FROM v_telemetry_valid tr
  JOIN metric_types mt USING (metric_id)
  JOIN assets a USING (asset_id)
  JOIN system_types st USING (system_type_id)
  JOIN locations l USING (location_id)
  JOIN buildings b USING (building_id)
  LEFT JOIN data_sources ds USING (source_id)
 WHERE b.building_id = %(building_id)s
   AND tr.ts >= %(start_utc)s AND tr.ts < %(end_utc)s
   AND mt.name = ANY(%(metric_names)s::text[])
   AND (%(systems)s::text[] IS NULL OR st.code = ANY(%(systems)s))
   AND (%(source)s::text IS NULL OR ds.source_code = %(source)s)
   AND tr.quality IN ('GOOD','UNCERTAIN')
   AND ((st.code = 'HVAC' AND mt.name = 'temperature' AND mt.unit='C')
     OR (st.code = 'NETWORKING' AND mt.name IN ('bandwidth_util','packet_loss') AND mt.unit='%%'))
 ORDER BY a.asset_id, normalized_score DESC, tr.ts DESC, tr.telemetry_id DESC
 LIMIT {MAX_TELEMETRY_ASSETS + 1}"""
SQL_UNSUPPORTED_UNITS = f"""
SELECT DISTINCT a.asset_id, a.asset_code
  FROM telemetry_records tr JOIN metric_types mt USING(metric_id)
  JOIN assets a USING(asset_id) JOIN system_types st USING(system_type_id)
  JOIN locations l USING(location_id) JOIN buildings b ON b.building_id=l.building_id
  LEFT JOIN data_sources ds ON ds.source_id=tr.source_id
 WHERE b.building_id=%(building_id)s AND tr.ts >= %(start_utc)s AND tr.ts < %(end_utc)s
   AND tr.quality IS DISTINCT FROM 'BAD'
   AND ((st.code='HVAC' AND mt.name='temperature' AND mt.unit<>'C') OR
        (st.code='NETWORKING' AND mt.name IN ('bandwidth_util','packet_loss') AND mt.unit<>'%%'))
   AND (%(systems)s::text[] IS NULL OR st.code = ANY(%(systems)s))
   AND (%(source)s::text IS NULL OR ds.source_code = %(source)s)
 LIMIT {MAX_TELEMETRY_ASSETS + 1}"""
SQL_UNKNOWN_QUALITY = f"""
SELECT tr.asset_id, count(*)::bigint AS unknown_quality_count
  FROM telemetry_records tr JOIN metric_types mt USING(metric_id)
  JOIN assets a USING(asset_id) JOIN system_types st USING(system_type_id)
  JOIN locations l USING(location_id) JOIN buildings b USING(building_id)
  LEFT JOIN data_sources ds ON ds.source_id=tr.source_id
 WHERE b.building_id=%(building_id)s AND tr.ts >= %(start_utc)s AND tr.ts < %(end_utc)s
   AND tr.quality IS NULL AND mt.name = ANY(%(metric_names)s::text[])
   AND ((st.code='HVAC' AND mt.name='temperature') OR
        (st.code='NETWORKING' AND mt.name IN ('bandwidth_util','packet_loss')))
   AND (%(systems)s::text[] IS NULL OR st.code = ANY(%(systems)s))
   AND (%(source)s::text IS NULL OR ds.source_code = %(source)s)
 GROUP BY tr.asset_id
 LIMIT {MAX_TELEMETRY_ASSETS + 1}"""


@dataclass(frozen=True)
class MaintenanceSourceCoverage:
    """Trusted server-side proof; never supplied by a tool caller/model."""
    supports_history_events: bool
    supports_history_telemetry: bool
    retrieval_confirmed_complete: bool
    covered_from: datetime | None
    covered_to: datetime | None
    data_as_of: datetime | None


MaintenanceCoverageProvider = Callable[
    [str, datetime, datetime, Sequence[str]], Mapping[str, MaintenanceSourceCoverage] | None
]


@dataclass(frozen=True)
class CoverageResult:
    complete: bool
    data_as_of: datetime | None
    limitations: tuple[Limitation, ...]


def assess_maintenance_coverage(provider: MaintenanceCoverageProvider | None, building: str,
                                sources: Sequence[str], start: datetime, end: datetime) -> CoverageResult:
    reports = provider(building, start, end, tuple(sources)) if provider and sources else None
    if not reports:
        reason = "no coverage provider is configured" if provider is None else "no mapped sources or no coverage information"
        return CoverageResult(False, None, (Limitation(
            LimitationCode.COMPLETION_NOT_CONFIRMED,
            "Event and telemetry retrieval completeness for the entire period is not confirmed; scores are withheld.",
            {"reason": reason, "sources": list(sources)}),))
    missing = []
    timestamps = []
    for code in sources:
        item = reports.get(code)
        if (item is None or not item.supports_history_events or not item.supports_history_telemetry
                or not item.retrieval_confirmed_complete or item.covered_from is None or item.covered_to is None
                or item.covered_from > start or item.covered_to < end):
            missing.append(code)
        elif item.data_as_of is not None:
            timestamps.append(item.data_as_of)
    if missing:
        return CoverageResult(False, None, (Limitation(
            LimitationCode.COMPLETION_NOT_CONFIRMED,
            "One or more sources do not confirm complete event and telemetry history; scores are withheld.",
            {"sources": sorted(missing)}),))
    return CoverageResult(True, min(timestamps) if timestamps else None, ())


def normalize_count_faults(count: int) -> float:
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("fault count must be a non-negative integer")
    return float(min(100, count * FAULT_PER_EVENT))


def normalize_condition(value: float, warning: float, critical: float) -> float:
    """0 at/below warning; 50 just above warning; linear to 100 at critical; clamp above critical."""
    if critical <= warning:
        raise ValueError("critical threshold must exceed warning threshold")
    if value <= warning:
        return 0.0
    return round(min(100.0, 50.0 + 50.0 * (value - warning) / (critical - warning)), 2)


def normalize_downtime(seconds: int | float) -> float:
    if seconds < 0:
        raise ValueError("downtime seconds cannot be negative")
    return round(min(100.0, 100.0 * seconds / OUTAGE_SATURATION_SECONDS), 2)


def calculate_priority(*, fault_count: int, downtime_seconds: int | float | None,
                       condition_score: float | None, availability_eligible: bool,
                       coverage_complete: bool, condition_supported: bool,
                       fault_identity_complete: bool = True,
                       policy: ScoringPolicy = DEFAULT_SCORING_POLICY) -> tuple[float | None, str, str, float, float | None]:
    fault_score = normalize_count_faults(fault_count)
    downtime_score = normalize_downtime(downtime_seconds or 0) if availability_eligible and downtime_seconds is not None else None
    if (not coverage_complete or not condition_supported or not fault_identity_complete
            or (availability_eligible and downtime_seconds is None)):
        return None, "INSUFFICIENT_DATA", ("availability-v1" if availability_eligible else "fault-condition-v1"), fault_score, downtime_score
    if condition_score is None:
        return None, "INSUFFICIENT_DATA", ("availability-v1" if availability_eligible else "fault-condition-v1"), fault_score, downtime_score
    if availability_eligible:
        wf, wd, wc = policy.availability_weights
        score = wf * fault_score + wd * downtime_score + wc * condition_score
        profile = "availability-v1"
    else:
        wf, wc = policy.fault_condition_weights
        score = wf * fault_score + wc * condition_score
        profile = "fault-condition-v1"
    score = round(score, 2)
    priority = "HIGH" if score >= policy.high_threshold else "MEDIUM" if score >= policy.medium_threshold else "LOW"
    return score, priority, profile, fault_score, downtime_score


def rank_within_profiles(rows: Sequence[Row]) -> list[Row]:
    """Deterministic ordering; scores from different profiles are intentionally never compared."""
    ordered = sorted(rows, key=lambda r: (r.values["scoring_profile"],
                                          -(r.values["score"] if r.values["score"] is not None else -1),
                                          r.values["building_code"], r.values["system_type"] or "",
                                          r.values["asset_code"]))
    ranks: dict[str, int] = {}
    ranked = []
    for row in ordered:
        profile = row.values["scoring_profile"]
        ranks[profile] = ranks.get(profile, 0) + 1
        ranked.append(Row({**row.values, "rank_within_profile": ranks[profile]}, row.evidence_refs))
    return ranked


def _echo(building, start, end, systems, source, period: Period | None = None):
    out = {"building": building, "start": start, "end": end, "systems": list(systems) if systems is not None else None,
           "source": source, "policy_version": DEFAULT_SCORING_POLICY.version}
    if period:
        out.update({"timezone": period.timezone_name, "start_utc": period.start_utc,
                    "end_utc_exclusive": period.end_utc_exclusive})
    return out


def run_maintenance_candidates(cur, *, building: str, start: str, end: str,
                               systems: Sequence[str] | None = None, source: str | None = None,
                               coverage_provider: MaintenanceCoverageProvider | None = None,
                               confirmed_timezone_buildings: frozenset[str] = frozenset(),
                               max_rows: int = 500, clock: Callable[[], datetime] | None = None,
                               scoring_policy: ScoringPolicy = DEFAULT_SCORING_POLICY) -> Envelope:
    """Run in a dict-row read-only transaction; database user must itself be SELECT-only."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    if not building or not isinstance(building, str):
        return cannot_compute(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters={}, columns=COLUMNS,
                              limitation=Limitation(LimitationCode.INVALID_PARAMETER, "building is required."), clock=clock)
    if max_rows < 1 or max_rows > 5000:
        return cannot_compute(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters={"building": building},
                              columns=COLUMNS, limitation=Limitation(LimitationCode.INVALID_PARAMETER,
                              "max_rows must be from 1 through 5000."), clock=clock)
    if systems is not None and (isinstance(systems, (str, bytes)) or not all(isinstance(x, str) for x in systems)):
        return cannot_compute(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters={"building": building},
                              columns=COLUMNS, limitation=Limitation(LimitationCode.INVALID_PARAMETER,
                              "systems must be a list of system codes."), clock=clock)
    systems = sorted({x.strip().upper() for x in systems}) if systems is not None else None
    source = source.strip() if isinstance(source, str) else source
    if source == "":
        return cannot_compute(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters={"building": building},
                              columns=COLUMNS, limitation=Limitation(LimitationCode.INVALID_PARAMETER,
                              "source must not be empty."), clock=clock)
    cur.execute(SQL_BUILDING, {"building": building})
    b = cur.fetchone()
    params = _echo(building, start, end, systems, source)
    params["policy_version"] = scoring_policy.version
    if not b:
        return cannot_compute(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=params, columns=COLUMNS,
                              limitation=Limitation(LimitationCode.INVALID_PARAMETER,
                              f"Unknown building code {building!r}."), clock=clock)
    try:
        period = build_period(start, end, b["timezone"])
    except PeriodError as exc:
        return cannot_compute(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=params, columns=COLUMNS,
                              limitation=Limitation(LimitationCode(exc.code), str(exc)), clock=clock)
    params = _echo(building, start, end, systems, source, period)
    params["policy_version"] = scoring_policy.version
    if (period.end_utc_exclusive - period.start_utc).total_seconds() > MAX_WINDOW_DAYS * 86400:
        return cannot_compute(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=params, columns=COLUMNS,
                              limitation=Limitation(LimitationCode.INVALID_PARAMETER,
                              f"Analysis window may not exceed {MAX_WINDOW_DAYS} days."), clock=clock)
    cur.execute(SQL_CATALOGUE_READY)
    if cur.fetchone()["n"] != 3:
        return cannot_compute(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=params, columns=COLUMNS,
                              limitation=Limitation(LimitationCode.CATALOGUE_NOT_MIGRATED,
                              "Event classes are unavailable; apply migration 014 before maintenance analysis."), clock=clock)
    cur.execute(SQL_SYSTEMS)
    known = {r["code"] for r in cur.fetchall()}
    unknown = sorted(set(systems or ()) - known)
    if unknown:
        return cannot_compute(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=params, columns=COLUMNS,
                              limitation=Limitation(LimitationCode.INVALID_PARAMETER,
                              f"Unknown system type code(s): {unknown}.", {"known_systems": sorted(known)}), clock=clock)
    if source is not None:
        cur.execute("SELECT 1 FROM data_sources WHERE building_id=%(building_id)s AND source_code=%(source)s",
                    {"building_id": b["building_id"], "source": source})
        if cur.fetchone() is None:
            return cannot_compute(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=params, columns=COLUMNS,
                                  limitation=Limitation(LimitationCode.INVALID_PARAMETER,
                                  f"Unknown source {source!r} in building {building!r}."), clock=clock)
    cur.execute(SQL_AVAILABILITY_SYSTEMS)
    availability_systems = {r["code"] for r in cur.fetchall()}
    binds = {"building_id": b["building_id"], "start_utc": period.start_utc, "end_utc": period.end_utc_exclusive,
             "systems": systems, "source": source}
    cur.execute(SQL_SOURCES, {"building_id": b["building_id"], "source": source,
                              "start_utc": period.start_utc, "end_utc": period.end_utc_exclusive})
    source_codes = [r["source_code"] for r in cur.fetchall()]
    coverage = assess_maintenance_coverage(coverage_provider, building, source_codes,
                                          period.start_utc, period.end_utc_exclusive)
    cur.execute(SQL_FAULTS, binds)
    fault_rows = list(cur.fetchall())
    if len(fault_rows) > MAX_FAULT_EVENTS:
        return cannot_compute(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=params, columns=COLUMNS,
                              limitation=Limitation(LimitationCode.ROWS_TRUNCATED,
                              "The fault event bound was exceeded; no partial score is returned.",
                              {"limit": MAX_FAULT_EVENTS}), clock=clock)
    cur.execute(SQL_CONDITION, {**binds, "metric_names": sorted({m for system in CONDITION_METRICS.values() for m in system}),
                                "hvac_warn": T["hvac_temp_warn"], "server_warn": T["server_temp_warn"],
                                "hvac_crit": T["hvac_temp_crit"], "server_crit": T["server_temp_crit"],
                                "bandwidth_warn": T["bandwidth_warn_pct"], "loss_warn": T["packet_loss_warn"],
                                "bandwidth_crit": T["bandwidth_crit_mean_pct"],
                                "loss_crit": T["packet_loss_crit_mean"]})
    condition_rows = list(cur.fetchall())
    if len(condition_rows) > MAX_TELEMETRY_ASSETS:
        return cannot_compute(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=params, columns=COLUMNS,
                              limitation=Limitation(LimitationCode.ROWS_TRUNCATED,
                              "The telemetry asset bound was exceeded; no partial score is returned.",
                              {"limit": MAX_TELEMETRY_ASSETS}), clock=clock)
    condition_params = {**binds, "metric_names": sorted({m for system in CONDITION_METRICS.values() for m in system})}
    cur.execute(SQL_UNKNOWN_QUALITY, condition_params)
    unknown_quality_rows = list(cur.fetchall())
    if len(unknown_quality_rows) > MAX_TELEMETRY_ASSETS:
        return cannot_compute(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=params, columns=COLUMNS,
                              limitation=Limitation(LimitationCode.ROWS_TRUNCATED,
                              "The unknown-quality telemetry asset bound was exceeded; no partial score is returned.",
                              {"limit": MAX_TELEMETRY_ASSETS}), clock=clock)
    unknown_quality_by_asset = {r["asset_id"]: int(r["unknown_quality_count"]) for r in unknown_quality_rows}

    # Reuse the established outage session state machine. Scope stays in this building and period.
    def outage_coverage(_building, s, e, codes):
        reports = coverage_provider(_building, s, e, tuple(codes)) if coverage_provider and codes else None
        return {code: SourceCoverage(code, bool(rep and rep.supports_history_events),
                                     bool(rep and rep.retrieval_confirmed_complete and rep.covered_from and rep.covered_to
                                          and rep.covered_from <= s and rep.covered_to >= e),
                                     rep.covered_from if rep else None, rep.covered_to if rep else None,
                                     rep.data_as_of if rep else None) for code in codes for rep in [reports.get(code) if reports else None]}

    outage_result = run_devices_offline_frequency(
        cur, building=building, start=start, end=end, systems=systems, source=source,
        coverage_provider=outage_coverage if coverage_provider else None,
        confirmed_timezone_buildings=confirmed_timezone_buildings, max_rows=MAX_TELEMETRY_ASSETS, clock=clock)
    outage_by_asset = {r.values["asset_code"]: r for r in outage_result.rows}

    grouped_faults: dict[int, list[Mapping[str, Any]]] = {}
    meta: dict[int, Mapping[str, Any]] = {}
    for event in fault_rows:
        grouped_faults.setdefault(event["asset_id"], []).append(event)
        meta[event["asset_id"]] = event
    for row in condition_rows:
        meta[row["asset_id"]] = row
    faults_by_code = {meta[aid]["asset_code"]: rows for aid, rows in grouped_faults.items()}
    condition_by_code = {r["asset_code"]: r for r in condition_rows}
    condition_candidate_codes = {r["asset_code"] for r in condition_rows if float(r["normalized_score"]) > 0}
    cur.execute(SQL_UNSUPPORTED_UNITS, binds)
    unsupported_unit_rows = list(cur.fetchall())
    if len(unsupported_unit_rows) > MAX_TELEMETRY_ASSETS:
        return cannot_compute(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=params, columns=COLUMNS,
                              limitation=Limitation(LimitationCode.ROWS_TRUNCATED,
                              "The unsupported-unit telemetry asset bound was exceeded; no partial score is returned.",
                              {"limit": MAX_TELEMETRY_ASSETS}), clock=clock)
    unsupported_unit_codes = sorted({r["asset_code"] for r in unsupported_unit_rows})
    all_codes = set(faults_by_code) | condition_candidate_codes | set(outage_by_asset)
    rows: list[Row] = []
    qualities = []
    data_limited_codes = []
    unknown_quality_codes = []
    missing_condition_codes = []
    missing_downtime_codes = []
    missing_fault_identity_codes = []
    for code in sorted(all_codes):
        fault_events = faults_by_code.get(code, [])
        fault_missing_identity_count = sum(1 for event in fault_events if not event["source_event_id"])
        fault_identity_complete = fault_missing_identity_count == 0
        cond = condition_by_code.get(code)
        outage = outage_by_asset.get(code)
        asset = meta.get(fault_events[0]["asset_id"]) if fault_events else cond
        if asset is None and outage:
            # Offline rows can exist without a fault/condition row. Obtain basic identity using a scoped parameterized lookup.
            cur.execute("""SELECT a.asset_id, a.asset_code, a.name AS asset_name, st.code AS system_type,
                                 ds.source_code, b.building_code
                            FROM assets a JOIN system_types st USING(system_type_id)
                            JOIN locations l USING(location_id) JOIN buildings b USING(building_id)
                            LEFT JOIN data_sources ds ON ds.source_code=%(source)s AND ds.building_id=b.building_id
                           WHERE b.building_id=%(building_id)s AND a.asset_code=%(asset_code)s""",
                        {"building_id": b["building_id"], "asset_code": code, "source": source})
            asset = cur.fetchone()
        if asset is None:
            continue
        system = asset.get("system_type")
        profile_eligible = system in availability_systems
        supported_metric_map = CONDITION_METRICS.get(system, {})
        condition_supported = bool(supported_metric_map)
        event_refs = [EvidenceRef("event_record", str(e["event_id"]), e["ts"], e["event_type"])
                      for e in fault_events]
        refs = list(event_refs)
        if outage:
            refs.extend(ref for ref in outage.evidence_refs if (ref.kind, ref.id) not in {(r.kind, r.id) for r in refs})
        downtime_usable = bool(outage and outage_result.complete
                               and not outage.values["duration_is_lower_bound"]
                               and outage.values["initial_state"] != "UNKNOWN"
                               and outage.values["orphan_offline_end_count"] == 0)
        condition_score = None
        condition_metric = condition_value = condition_unit = condition_quality = None
        if cond is not None:
            metric_def = supported_metric_map.get(cond["metric"])
            if metric_def and cond["unit"] == metric_def[0]:
                condition_score = float(cond["normalized_score"])
                condition_metric, condition_value, condition_unit = cond["metric"], float(cond["value"]), cond["unit"]
                condition_quality = cond["quality"]
                if condition_quality == "UNCERTAIN":
                    qualities.append("UNCERTAIN")
                refs.append(EvidenceRef("telemetry_record", str(cond["telemetry_id"]), cond["ts"],
                                        metric_code=cond["metric"], value=float(cond["value"]),
                                        unit=cond["unit"], quality=cond["quality"]))
        has_unknown_quality = bool(unknown_quality_by_asset.get(asset["asset_id"], 0))
        if condition_score is None and cond is not None and condition_supported and coverage.complete and not has_unknown_quality:
            condition_score = 0.0  # observed in-period canonical readings confirm no threshold excursion
        elif has_unknown_quality:
            condition_score = None
            condition_quality = "UNKNOWN"
            unknown_quality_codes.append(code)
        if not condition_supported:
            data_limited_codes.append(code)
        elif cond is None and not has_unknown_quality and code not in unsupported_unit_codes:
            missing_condition_codes.append(code)
        if profile_eligible and not downtime_usable:
            missing_downtime_codes.append(code)
        downtime = int(outage.values["downtime_seconds_in_period"]) if outage else None
        scoring_downtime = downtime if downtime_usable else None
        score, priority, profile, fault_score, downtime_score = calculate_priority(
            fault_count=len(fault_events), downtime_seconds=scoring_downtime, condition_score=condition_score,
            availability_eligible=profile_eligible, coverage_complete=coverage.complete,
            condition_supported=condition_supported and not has_unknown_quality and code not in unsupported_unit_codes
                               and cond is not None, fault_identity_complete=fault_identity_complete,
            policy=scoring_policy)
        reasons = []
        if fault_events:
            reasons.append(f"{len(fault_events)} independent FAULT-class open event(s) in the period")
        if fault_missing_identity_count:
            missing_fault_identity_codes.append(code)
            reasons.append(f"{fault_missing_identity_count} fault event(s) lack source identity; recurrence score withheld")
        if condition_metric is not None and condition_score is not None and condition_score > 0:
            reasons.append(f"{condition_metric} reading {condition_value:g} {condition_unit} exceeded its configured warning threshold")
        if condition_supported and cond is None:
            reasons.append("condition factor unavailable: no supported canonical measurement was observed in the period")
        if profile_eligible and not downtime_usable:
            reasons.append("downtime factor unavailable: complete non-lower-bound session evidence was not confirmed")
        if outage and outage.values["downtime_seconds_in_period"]:
            reasons.append(f"{outage.values['downtime_seconds_in_period']} seconds of explicit offline sessions (context from devices.offline_frequency)")
        if not coverage.complete:
            reasons.append("score withheld because source history completeness is unconfirmed")
        values = {
            "building_code": building, "asset_code": code, "asset_name": asset["asset_name"],
            "system_type": system, "source_code": asset.get("source_code"),
            "source_codes": sorted({str(x) for x in [asset.get("source_code"),
                           *(e.get("source_code") for e in fault_events), cond.get("source_code") if cond else None,
                           outage.values.get("source_code") if outage else None] if x}),
            "scoring_profile": profile,
            "priority": priority, "score": score, "policy_version": scoring_policy.version,
            "rank_within_profile": None,
            "fault_count": len(fault_events), "fault_score": fault_score,
            "fault_missing_identity_count": fault_missing_identity_count,
            "fault_identity_complete": fault_identity_complete,
            "downtime_seconds": downtime, "downtime_score": downtime_score,
            "condition_metric": condition_metric, "condition_value": condition_value,
            "condition_unit": condition_unit, "condition_score": condition_score,
            "condition_quality": condition_quality,
            "data_sufficiency": "CONFIRMED" if score is not None and condition_quality in (None, "GOOD")
                                else "LIMITED" if score is not None and condition_quality == "UNCERTAIN"
                                else "INSUFFICIENT",
            "maintenance_context": "UNAVAILABLE: no maintenance history/work-order data is stored",
            "reasons": reasons,
        }
        rows.append(Row(values, tuple(refs)))
    rows = rank_within_profiles(rows)
    limitations = list(coverage.limitations)
    if data_limited_codes:
        limitations.append(Limitation(LimitationCode.CAPABILITY_UNSUPPORTED,
                                      "No approved condition thresholds are defined for these systems; their score is withheld.",
                                      {"assets": sorted(data_limited_codes)}))
    if unknown_quality_codes:
        limitations.append(Limitation(LimitationCode.COMPLETION_NOT_CONFIRMED,
                                      "Legacy NULL-quality condition readings exist for these assets; scores depending on the condition factor are withheld.",
                                      {"assets": sorted(unknown_quality_codes)}))
    if unsupported_unit_codes:
        limitations.append(Limitation(LimitationCode.CAPABILITY_UNSUPPORTED,
                                      "Condition measurements with non-canonical units were excluded; affected asset scores are withheld.",
                                      {"assets": unsupported_unit_codes}))
    if missing_condition_codes:
        limitations.append(Limitation(LimitationCode.COMPLETION_NOT_CONFIRMED,
                                      "No supported canonical condition measurement was observed for these candidates; no zero condition score was assumed.",
                                      {"assets": sorted(missing_condition_codes)}))
    if missing_downtime_codes:
        limitations.append(Limitation(LimitationCode.COMPLETION_NOT_CONFIRMED,
                                      "Complete non-lower-bound outage evidence was unavailable for these candidates; no zero downtime was assumed.",
                                      {"assets": sorted(missing_downtime_codes)}))
    if missing_fault_identity_codes:
        limitations.append(Limitation(LimitationCode.MISSING_EVENT_IDENTITY,
                                      "Some fault events lack source_event_id, so duplicate delivery cannot be excluded and scores depending on fault recurrence are withheld.",
                                      {"assets": sorted(missing_fault_identity_codes)}))
    if any(q == "UNCERTAIN" for q in qualities):
        limitations.append(Limitation(LimitationCode.COMPLETION_NOT_CONFIRMED,
                                      "UNCERTAIN measurements contributed to condition scores; those rows have limited sufficiency."))
    if period.timezone_name == BACKFILL_DEFAULT_TIMEZONE and building not in confirmed_timezone_buildings:
        limitations.append(Limitation(LimitationCode.TIMEZONE_PROVENANCE_UNCONFIRMED,
                                      "Building timezone provenance is unconfirmed; local date boundaries may be wrong around DST.",
                                      {"timezone": period.timezone_name}))
    status = ResultStatus.PARTIAL if limitations else ResultStatus.COMPLETE
    return EnvelopeBuilder(tool=TOOL_NAME, query_version=TOOL_VERSION, parameters=params,
                           data_as_of=coverage.data_as_of, source_ids=source_codes, columns=COLUMNS,
                           rows=rows, limitations=limitations, status=status, quality="UNCERTAIN" if qualities else None,
                           max_rows=max_rows, clock=clock).build()


def build_maintenance_candidates_spec(cursor_factory: Callable[[], AbstractContextManager], *,
                                      coverage_provider: MaintenanceCoverageProvider | None = None,
                                      confirmed_timezone_buildings: frozenset[str] = frozenset(),
                                      max_rows: int = 500,
                                      clock: Callable[[], datetime] | None = None,
                                      scoring_policy: ScoringPolicy = DEFAULT_SCORING_POLICY) -> ToolSpec:
    """Trusted coverage/timezone policy are server wiring and are deliberately not model arguments."""
    def handler(**arguments: Any) -> Envelope:
        with cursor_factory() as cur:
            return run_maintenance_candidates(cur, coverage_provider=coverage_provider,
                                              confirmed_timezone_buildings=confirmed_timezone_buildings,
                                              max_rows=max_rows, clock=clock, scoring_policy=scoring_policy, **arguments)
    return ToolSpec(TOOL_NAME, DESCRIPTION, PARAMETERS_SCHEMA, handler, TOOL_VERSION)
