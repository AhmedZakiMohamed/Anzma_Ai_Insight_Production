"""Authenticated, read-only HTTP views over the persisted E3/E4 freshness snapshots."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from psycopg import OperationalError

from app.adapters.database import db_adapter as da
from app.config.freshness_api import snapshot_max_age_seconds
from app.repositories.dashboard_core_pg import open_readonly


LOGGER = logging.getLogger("anzma.freshness")
MAX_LIMIT = 500
MAX_CONNECTOR_STATUS_BYTES = 8 * 1024
FRESHNESS_STATES = {"FRESH", "STALE", "MISSING", "UNKNOWN"}
LIVENESS_STATES = {"ALIVE", "SILENT", "UNKNOWN", "DISABLED"}
GAP_CAUSES = {"UNKNOWN", "NO_DATA", "SOURCE_SUSPECTED"}
GAP_STATUSES = {"open", "closed", "all"}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("persisted timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="auto").replace("+00:00", "Z")


def _pagination(limit: int, offset: int) -> None:
    if not 1 <= limit <= MAX_LIMIT:
        raise HTTPException(400, f"limit must be between 1 and {MAX_LIMIT}.")
    if offset < 0:
        raise HTTPException(400, "offset must be zero or greater.")


def _choice(value: str | None, allowed: set[str], field: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    if normalized not in allowed:
        raise HTTPException(400, f"Invalid {field} filter.")
    return normalized


def _since(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise HTTPException(400, "since must be an RFC 3339 timestamp with a timezone.") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise HTTPException(400, "since must include a timezone.")
    return result.astimezone(timezone.utc)


def _snapshot(conn: Any, now: datetime) -> dict[str, Any]:
    row = conn.execute(
        """SELECT (SELECT max(evaluated_at) FROM series_freshness_current) AS series_at,
                  (SELECT max(evaluated_at) FROM source_liveness_current) AS source_at"""
    ).fetchone()
    evaluated_at = max((value for value in (row["series_at"], row["source_at"]) if value is not None), default=None)
    if evaluated_at is None:
        return {"evaluated_at": None, "evaluation_age_seconds": None, "snapshot_stale": True}
    age = max(0, int((now - evaluated_at).total_seconds()))
    return {"evaluated_at": _iso(evaluated_at), "evaluation_age_seconds": age,
            "snapshot_stale": age > snapshot_max_age_seconds()}


def _filter_sql(filters: list[tuple[str, Any]], alias: str, fields: dict[str, str]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for key, value in filters:
        if value is not None:
            clauses.append(f"{fields[key]}=%s")
            params.append(value)
    return (" AND ".join(clauses) if clauses else "TRUE", params)


def _connector_status(value: Any) -> tuple[Any, bool]:
    if value is None:
        return None, False
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        LOGGER.warning("Omitting unreadable connector_status from freshness response.")
        return None, True
    if len(payload) > MAX_CONNECTOR_STATUS_BYTES:
        LOGGER.warning("Omitting oversized connector_status from freshness response.")
        return None, True
    return value, False


def _http_db_error(exc: Exception) -> HTTPException:
    return HTTPException(503, f"Database unavailable: {str(exc).strip()[:200]}")


def _selected_building(open_ro, building: str | None, building_code: str | None) -> str | None:
    """Support the shared `building` alias while preserving the older building_code filter."""
    if building in (None, ""):
        return building_code
    if building_code not in (None, "", building):
        raise HTTPException(400, "building and building_code filters must match.")
    try:
        with open_ro() as conn:
            found = conn.execute("SELECT 1 FROM buildings WHERE building_code=%s", (building,)).fetchone()
    except (OperationalError, da.DatabaseUnavailable) as exc:
        raise _http_db_error(exc) from exc
    if found is None:
        raise HTTPException(400, f"Unknown building_code: {building}.")
    return building


def create_router(open_ro=open_readonly, clock=lambda: datetime.now(timezone.utc)):
    router = APIRouter(prefix="/api/v1", tags=["Freshness (read-only)"])

    @router.get("/freshness", summary="Persisted series freshness")
    def freshness(
        building: str | None = None,
        building_code: str | None = None,
        source_code: str | None = None,
        asset_code: str | None = None,
        metric_code: str | None = None,
        state: str | None = None,
        limit: int = Query(100),
        offset: int = Query(0),
    ):
        _pagination(limit, offset)
        selected_state = _choice(state, FRESHNESS_STATES, "state")
        selected_building = _selected_building(open_ro, building, building_code)
        filters = [("building_code", selected_building), ("source_code", source_code),
                   ("asset_code", asset_code), ("metric_code", metric_code), ("state", selected_state)]
        fields = {"building_code": "b.building_code", "source_code": "ds.source_code",
                  "asset_code": "a.asset_code", "metric_code": "f.metric_code", "state": "f.freshness"}
        where, params = _filter_sql(filters, "", fields)
        joins = """FROM series_freshness_current f
                   JOIN assets a USING(asset_id)
                   JOIN locations l USING(location_id)
                   JOIN buildings b USING(building_id)
                   LEFT JOIN data_sources ds ON ds.source_id=f.source_id
                   LEFT JOIN LATERAL (
                       SELECT g.gap_start,g.detected_at,g.expected_interval_seconds,g.opened_in_work_hours,g.cause
                         FROM data_gaps g
                        WHERE g.source_id IS NOT DISTINCT FROM f.source_id AND g.asset_id=f.asset_id
                          AND g.record_kind=f.record_kind AND g.metric_code IS NOT DISTINCT FROM f.metric_code
                          AND g.event_type IS NOT DISTINCT FROM f.event_type AND g.gap_end IS NULL
                        ORDER BY g.detected_at DESC,g.gap_id DESC LIMIT 1
                   ) og ON TRUE"""
        try:
            with open_ro() as conn:
                snapshot = _snapshot(conn, clock().astimezone(timezone.utc))
                total = conn.execute(
                    f"SELECT count(*) AS total {joins} WHERE a.lifecycle_status='ACTIVE' AND {where}", params
                ).fetchone()["total"]
                counts = conn.execute(
                    f"""SELECT f.freshness,count(*) AS count {joins}
                         WHERE a.lifecycle_status='ACTIVE' AND {where} GROUP BY f.freshness""", params
                ).fetchall()
                rows = conn.execute(
                    f"""SELECT b.building_code,ds.source_code,a.asset_code,f.record_kind,f.metric_code,f.event_type,
                               f.freshness,f.last_observed_at,f.last_received_at,f.age_seconds,
                               f.expected_interval_seconds,f.evaluated_at,
                               og.gap_start AS open_gap_start,og.detected_at AS open_gap_detected_at,
                               og.expected_interval_seconds AS open_gap_expected_interval,
                               og.opened_in_work_hours AS open_gap_opened_in_work_hours,og.cause AS open_gap_cause
                          {joins}
                         WHERE a.lifecycle_status='ACTIVE' AND {where}
                         ORDER BY b.building_code NULLS FIRST,ds.source_code NULLS FIRST,a.asset_code,
                                  f.record_kind,f.metric_code NULLS FIRST,f.event_type NULLS FIRST
                         LIMIT %s OFFSET %s""", (*params, limit, offset)
                ).fetchall()
        except (OperationalError, da.DatabaseUnavailable) as exc:
            raise _http_db_error(exc) from exc
        items = []
        for row in rows:
            open_gap = None
            if row["open_gap_start"] is not None:
                open_gap = {"gap_start": _iso(row["open_gap_start"]), "detected_at": _iso(row["open_gap_detected_at"]),
                            "expected_interval_seconds": row["open_gap_expected_interval"],
                            "opened_in_work_hours": row["open_gap_opened_in_work_hours"], "cause": row["open_gap_cause"]}
            items.append({"building_code": row["building_code"], "source_code": row["source_code"],
                          "asset_code": row["asset_code"], "record_kind": row["record_kind"],
                          "metric_code": row["metric_code"], "event_type": row["event_type"],
                          "freshness": row["freshness"], "last_observed_at": _iso(row["last_observed_at"]),
                          "last_received_at": _iso(row["last_received_at"]), "age_seconds": row["age_seconds"],
                          "expected_interval_seconds": row["expected_interval_seconds"],
                          "evaluated_at": _iso(row["evaluated_at"]), "open_gap": open_gap})
        state_counts = {name: 0 for name in sorted(FRESHNESS_STATES)}
        state_counts.update({row["freshness"]: row["count"] for row in counts})
        next_offset = offset + len(items) if offset + len(items) < total else None
        return {**snapshot, "state_counts": state_counts, "limit": limit, "offset": offset, "total": total,
                "next_offset": next_offset, "items": items}

    @router.get("/sources/liveness", summary="Persisted source liveness and separate diagnostics")
    def source_liveness(
        building: str | None = None,
        building_code: str | None = None,
        source_code: str | None = None,
        state: str | None = None,
        limit: int = Query(100),
        offset: int = Query(0),
    ):
        _pagination(limit, offset)
        selected_state = _choice(state, LIVENESS_STATES, "state")
        selected_building = _selected_building(open_ro, building, building_code)
        filters = [("building_code", selected_building), ("source_code", source_code), ("state", selected_state)]
        fields = {"building_code": "b.building_code", "source_code": "ds.source_code", "state": "s.state"}
        where, params = _filter_sql(filters, "", fields)
        joins = """FROM source_liveness_current s
                   JOIN data_sources ds USING(source_id)
                   JOIN buildings b USING(building_id)"""
        try:
            with open_ro() as conn:
                snapshot = _snapshot(conn, clock().astimezone(timezone.utc))
                total = conn.execute(f"SELECT count(*) AS total {joins} WHERE {where}", params).fetchone()["total"]
                rows = conn.execute(
                    f"""SELECT b.building_code,ds.source_code,s.state,s.reason_code,s.reason_message,s.evaluated_at,
                               s.last_received_at,s.age_seconds,s.expected_interval_seconds,
                               s.active_series_count,s.stale_series_count,s.missing_series_count,s.mass_stale,
                               s.last_success_at,s.last_error_at,s.last_error,s.connector_status
                          {joins} WHERE {where}
                         ORDER BY b.building_code,ds.source_code LIMIT %s OFFSET %s""", (*params, limit, offset)
                ).fetchall()
        except (OperationalError, da.DatabaseUnavailable) as exc:
            raise _http_db_error(exc) from exc
        items = []
        for row in rows:
            connector_status, omitted = _connector_status(row["connector_status"])
            last_error = row["last_error"]
            truncated = last_error is not None and len(last_error) > 500
            items.append({"building_code": row["building_code"], "source_code": row["source_code"],
                          "state": row["state"], "reason_code": row["reason_code"],
                          "reason_message": row["reason_message"], "evaluated_at": _iso(row["evaluated_at"]),
                          "last_received_at": _iso(row["last_received_at"]), "age_seconds": row["age_seconds"],
                          "expected_interval_seconds": row["expected_interval_seconds"],
                          "active_series_count": row["active_series_count"],
                          "stale_series_count": row["stale_series_count"],
                          "missing_series_count": row["missing_series_count"], "mass_stale": row["mass_stale"],
                          "last_success_at": _iso(row["last_success_at"]), "last_error_at": _iso(row["last_error_at"]),
                          "last_error": last_error[:500] if truncated else last_error,
                          "last_error_truncated": truncated, "connector_status": connector_status,
                          "connector_status_omitted": omitted})
        next_offset = offset + len(items) if offset + len(items) < total else None
        return {**snapshot, "limit": limit, "offset": offset, "total": total,
                "next_offset": next_offset, "items": items}

    @router.get("/data-gaps", summary="Persisted data-gap evidence and cause history")
    def data_gaps(
        status: str = "open",
        cause: str | None = None,
        building: str | None = None,
        building_code: str | None = None,
        source_code: str | None = None,
        asset_code: str | None = None,
        since: str | None = None,
        limit: int = Query(100),
        offset: int = Query(0),
    ):
        _pagination(limit, offset)
        selected_status = status.strip().lower()
        if selected_status not in GAP_STATUSES:
            raise HTTPException(400, "status must be open, closed, or all.")
        selected_cause = _choice(cause, GAP_CAUSES, "cause")
        since_time = _since(since)
        selected_building = _selected_building(open_ro, building, building_code)
        filters = [("building_code", selected_building), ("source_code", source_code), ("asset_code", asset_code),
                   ("cause", selected_cause), ("since", since_time)]
        fields = {"building_code": "b.building_code", "source_code": "ds.source_code",
                  "asset_code": "a.asset_code", "cause": "g.cause", "since": "g.detected_at >="}
        clauses = []
        params = []
        for key, value in filters:
            if value is not None:
                if key == "since":
                    clauses.append(f"{fields[key]} %s")
                else:
                    clauses.append(f"{fields[key]}=%s")
                params.append(value)
        if selected_status == "open":
            clauses.append("g.gap_end IS NULL")
        elif selected_status == "closed":
            clauses.append("g.gap_end IS NOT NULL")
        where = " AND ".join(clauses) if clauses else "TRUE"
        joins = """FROM data_gaps g JOIN assets a USING(asset_id)
                   JOIN locations l USING(location_id) JOIN buildings b ON b.building_id=l.building_id
                   LEFT JOIN data_sources ds ON ds.source_id=g.source_id"""
        try:
            with open_ro() as conn:
                rows = conn.execute(
                    f"""SELECT g.gap_id,b.building_code,ds.source_code,a.asset_code,g.record_kind,g.metric_code,
                               g.event_type,g.gap_start,g.gap_end,g.detected_at,g.expected_interval_seconds,
                               g.opened_in_work_hours,g.cause,g.cause_updated_at,g.closed_by,g.closed_at,
                               g.last_evaluated_at
                          {joins} WHERE {where}
                         ORDER BY g.detected_at DESC,g.gap_id DESC LIMIT %s OFFSET %s""", (*params, limit, offset)
                ).fetchall()
                total = conn.execute(f"SELECT count(*) AS total {joins} WHERE {where}", params).fetchone()["total"]
                ids = [row["gap_id"] for row in rows]
                history_rows = conn.execute(
                    """SELECT gap_id,old_cause,new_cause,changed_at FROM data_gap_cause_history
                        WHERE gap_id = ANY(%s) ORDER BY gap_id,changed_at,cause_history_id""", (ids,)
                ).fetchall() if ids else []
        except (OperationalError, da.DatabaseUnavailable) as exc:
            raise _http_db_error(exc) from exc
        histories: dict[int, list[dict[str, Any]]] = {}
        for entry in history_rows:
            histories.setdefault(entry["gap_id"], []).append({"old_cause": entry["old_cause"],
                "new_cause": entry["new_cause"], "changed_at": _iso(entry["changed_at"])})
        items = [{"building_code": row["building_code"], "source_code": row["source_code"],
                  "asset_code": row["asset_code"], "record_kind": row["record_kind"],
                  "metric_code": row["metric_code"], "event_type": row["event_type"],
                  "gap_start": _iso(row["gap_start"]), "gap_end": _iso(row["gap_end"]),
                  "detected_at": _iso(row["detected_at"]),
                  "expected_interval_seconds": row["expected_interval_seconds"],
                  "opened_in_work_hours": row["opened_in_work_hours"], "cause": row["cause"],
                  "cause_updated_at": _iso(row["cause_updated_at"]), "closed_by": row["closed_by"],
                  "closed_at": _iso(row["closed_at"]), "last_evaluated_at": _iso(row["last_evaluated_at"]),
                  "cause_history": histories.get(row["gap_id"], [])} for row in rows]
        next_offset = offset + len(items) if offset + len(items) < total else None
        return {"status": selected_status, "limit": limit, "offset": offset, "total": total,
                "next_offset": next_offset, "items": items}

    return router
