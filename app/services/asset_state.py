"""Persist the G3 asset-state projection in shadow mode.

The dashboard's existing status query remains authoritative. This evaluator
copies its operational bucket and adds availability/communication evidence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from decimal import Decimal
from typing import Any

from app.adapters.database import db_adapter as da
from app.domain.asset_state import derive_asset_state
from app.repositories.dashboard_core_pg import asset_status_rows


LOGGER = logging.getLogger("anzma.asset_state")
EVIDENCE_SCHEMA_VERSION = 1
AVAILABILITY_PAIRS = {
    "camera_offline": "UNAVAILABLE",
    "camera_online": "AVAILABLE",
    "link_down": "UNAVAILABLE",
    "link_up": "AVAILABLE",
}


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "item"):
        return value.item()
    return value


def _execute(target: Any, statement: str, params: Any = None) -> Any:
    """Return a cursor for psycopg2 and psycopg3 connection/cursor APIs."""
    if callable(getattr(target, "cursor", None)):
        cursor = target.cursor()
        cursor.execute(statement, params)
        return cursor
    result = target.execute(statement, params)
    return target if result is None else result


class _ConnectionExecuteAdapter:
    """Expose psycopg2 connection queries using the dashboard's execute API."""
    def __init__(self, conn: Any):
        self.conn = conn

    def execute(self, statement: str, params: Any = None) -> Any:
        return _execute(self.conn, statement, params)


def _dashboard_window(conn: Any) -> tuple[datetime, datetime]:
    row = _execute(conn,
        """SELECT max(ts) AS last FROM (
                 SELECT max(ts) AS ts FROM v_telemetry_valid
                 UNION ALL SELECT max(ts) AS ts FROM event_records
             ) observed"""
    ).fetchone()
    last = row["last"]
    if last is None:
        raise ValueError("cannot evaluate asset state without stored observations")
    end = last + timedelta(seconds=1)
    return last - timedelta(hours=24), end


def _availability(conn: Any, asset_id: int) -> tuple[str | None, dict[str, Any] | None]:
    row = _execute(conn,
        """SELECT et.name AS event_type, er.event_id, er.ts, er.severity
             FROM event_records er JOIN event_types et USING (event_type_id)
            WHERE er.asset_id=%s AND et.name = ANY(%s)
            ORDER BY er.ts DESC, er.event_id DESC LIMIT 1""",
        (asset_id, list(AVAILABILITY_PAIRS)),
    ).fetchone()
    if row is None:
        return None, None
    return AVAILABILITY_PAIRS[row["event_type"]], {
        "event_ref": f"event:{row['event_id']}", "event_type": row["event_type"],
        "observed_at": row["ts"], "severity": row["severity"],
    }


def _evidence(conn: Any, asset_id: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    series_rows = _execute(conn,
        """SELECT source_id, record_kind, metric_code, event_type, freshness,
                     last_observed_at, last_received_at, age_seconds,
                     expected_interval_seconds, expectation_id, evaluated_at
             FROM series_freshness_current
            WHERE asset_id=%s ORDER BY record_kind,metric_code,event_type,source_id NULLS FIRST""",
        (asset_id,),
    ).fetchall()
    series = [dict(row) for row in series_rows]
    source_rows = _execute(conn,
        """SELECT source_id FROM series_freshness_current WHERE asset_id=%s AND source_id IS NOT NULL
           UNION SELECT source_id FROM asset_external_ids WHERE asset_id=%s
           UNION SELECT source_id FROM telemetry_records WHERE asset_id=%s AND source_id IS NOT NULL
           UNION SELECT source_id FROM event_records WHERE asset_id=%s AND source_id IS NOT NULL
           UNION SELECT source_id FROM equipment_state_records WHERE asset_id=%s AND source_id IS NOT NULL""",
        (asset_id, asset_id, asset_id, asset_id, asset_id),
    ).fetchall()
    source_ids = sorted({row["source_id"] for row in source_rows})
    sources = []
    for source_id in source_ids:
        row = _execute(conn,
            """SELECT ds.source_id,ds.source_code,ds.status,ds.last_received_at,
                      ds.last_success_at,ds.last_error_at,ds.last_error,
                      sl.state AS liveness,sl.evaluated_at AS liveness_evaluated_at,
                      sl.expected_interval_seconds,sl.reason_code AS liveness_reason,
                      ib.connector_status,ib.delivery_mode
                 FROM data_sources ds
                 LEFT JOIN source_liveness_current sl USING (source_id)
                 LEFT JOIN LATERAL (
                     SELECT connector_status,delivery_mode FROM ingestion_batches b
                      WHERE b.source_id=ds.source_id
                      ORDER BY b.received_at DESC,b.ingestion_batch_id DESC LIMIT 1
                 ) ib ON true
                WHERE ds.source_id=%s""",
            (source_id,),
        ).fetchone()
        if row:
            sources.append(dict(row))

    # The compatibility view intentionally includes UNCERTAIN and legacy NULL
    # quality but excludes BAD; retain only the newest provenance row per series.
    measurements = _execute(conn,
        """SELECT DISTINCT ON (t.source_id,t.metric_id) t.telemetry_id,t.source_id,
                     m.name AS metric_code,t.ts,t.received_at,t.quality,t.value
             FROM v_telemetry_valid t JOIN metric_types m USING (metric_id)
            WHERE t.asset_id=%s
            ORDER BY t.source_id,t.metric_id,t.ts DESC,t.telemetry_id DESC""",
        (asset_id,),
    ).fetchall()
    measurement_evidence = [dict(row) for row in measurements]
    uncertain_refs = [f"telemetry:{row['telemetry_id']}" for row in measurements
                      if row["quality"] == "UNCERTAIN"]
    legacy_refs = [f"telemetry:{row['telemetry_id']}" for row in measurements
                   if row["quality"] is None or row["source_id"] is None or row["received_at"] is None]
    return series, {
        "source_ids": source_ids,
        "series": series,
        "sources": sources,
        "measurements": measurement_evidence,
        "uncertain_refs": uncertain_refs,
        "legacy_or_unattributed_refs": legacy_refs,
    }


def run_once(conn: Any, evaluated_at: datetime) -> dict[str, int]:
    """Evaluate and commit one complete shadow snapshot transactionally."""
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("evaluated_at must be timezone-aware")
    instant = evaluated_at.astimezone(timezone.utc)
    counts = {"assets_evaluated": 0, "baselines_written": 0, "transitions_written": 0}
    state_lock_acquired = False
    try:
        # Separate transaction from the already-committed freshness pass.
        conn.rollback()
        with conn.cursor() as lock_cur:
            lock_cur.execute("SELECT pg_advisory_lock(718230644)")
        conn.commit()
        state_lock_acquired = True
        _execute(conn, "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        with conn.cursor() as cur:
            start, end = _dashboard_window(conn)
            dashboard_conn = conn if callable(getattr(conn, "execute", None)) else _ConnectionExecuteAdapter(conn)
            buckets = {row["asset_id"]: row["status"] for row in asset_status_rows(dashboard_conn, start, end)}
            assets = _execute(conn, "SELECT asset_id FROM assets ORDER BY asset_id").fetchall()
            for asset in assets:
                asset_id = asset["asset_id"]
                series, evidence = _evidence(conn, asset_id)
                availability, availability_evidence = _availability(conn, asset_id)
                evidence["availability_event"] = availability_evidence
                projected = derive_asset_state(buckets.get(asset_id, "unknown"), series,
                                               availability, instant, evidence["source_ids"])
                evidence["dashboard_bucket"] = buckets.get(asset_id, "unknown")
                evidence["source_ids"] = projected["source_ids"]
                evidence["attribution_ambiguous"] = projected["attribution_ambiguous"]
                evidence = _json_value(evidence)
                previous = _execute(cur,
                    """SELECT operational_state,availability,communication_status,state_since
                         FROM asset_state_current WHERE asset_id=%s FOR UPDATE""", (asset_id,)
                ).fetchone()
                if previous is None:
                    state_since = instant
                    _execute(cur,
                        """INSERT INTO asset_state_current
                               (asset_id,operational_state,availability,communication_status,evaluated_at,
                                state_since,reason_code,reason_message,evidence_summary,evidence_schema_version)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1)""",
                        (asset_id, projected["operational_state"], projected["availability"],
                         projected["communication_status"], instant, state_since, projected["reason_code"],
                         projected["reason_message"], da.Json(cur, evidence)),
                    )
                    _execute(cur,
                        """INSERT INTO asset_state_history
                               (asset_id,kind,evaluated_at,to_operational_state,to_availability,
                                to_communication_status,state_since,reason_code,reason_message,
                                evidence_summary,evidence_schema_version)
                           VALUES (%s,'BASELINE',%s,%s,%s,%s,%s,%s,%s,%s,1)""",
                        (asset_id, instant, projected["operational_state"], projected["availability"],
                         projected["communication_status"], state_since, projected["reason_code"],
                         projected["reason_message"], da.Json(cur, evidence)),
                    )
                    counts["baselines_written"] += 1
                else:
                    old_semantics = (previous["operational_state"], previous["availability"],
                                     previous["communication_status"])
                    new_semantics = (projected["operational_state"], projected["availability"],
                                     projected["communication_status"])
                    state_since = instant if old_semantics != new_semantics else previous["state_since"]
                    _execute(cur,
                        """UPDATE asset_state_current SET operational_state=%s,availability=%s,
                                  communication_status=%s,evaluated_at=%s,state_since=%s,
                                  reason_code=%s,reason_message=%s,evidence_summary=%s,
                                  evidence_schema_version=1 WHERE asset_id=%s""",
                        (*new_semantics, instant, state_since, projected["reason_code"],
                         projected["reason_message"], da.Json(cur, evidence), asset_id),
                    )
                    if old_semantics != new_semantics:
                        _execute(cur,
                            """INSERT INTO asset_state_history
                                   (asset_id,kind,evaluated_at,from_operational_state,to_operational_state,
                                    from_availability,to_availability,from_communication_status,
                                    to_communication_status,state_since,reason_code,reason_message,
                                    evidence_summary,evidence_schema_version)
                               VALUES (%s,'TRANSITION',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)""",
                            (asset_id, instant,
                             old_semantics[0], new_semantics[0],
                             old_semantics[1], new_semantics[1],
                             old_semantics[2], new_semantics[2], state_since,
                             projected["reason_code"], projected["reason_message"], da.Json(cur, evidence)),
                        )
                        counts["transitions_written"] += 1
                counts["assets_evaluated"] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if state_lock_acquired:
            try:
                with conn.cursor() as unlock_cur:
                    unlock_cur.execute("SELECT pg_advisory_unlock(718230644)")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    LOGGER.info(json.dumps({"event": "ASSET_STATE_PASS", "evaluated_at": instant.isoformat(), **counts},
                           separators=(",", ":")))
    return counts


def alert_evidence_context(cur: Any, alert: dict[str, Any]) -> dict[str, Any] | None:
    """Build a private, reference-linked data-path context for one alert explanation."""
    cur.execute("SELECT to_regclass('public.asset_state_current') AS table_name")
    if cur.fetchone()["table_name"] is None:
        return None
    cur.execute(
        """SELECT operational_state,availability,communication_status,evaluated_at,
                  state_since,reason_code,evidence_summary
             FROM asset_state_current WHERE asset_id=%s""",
        (alert["asset_id"],),
    )
    snapshot = cur.fetchone()
    if snapshot is None:
        return None
    summary = snapshot["evidence_summary"] or {}
    source_rows = summary.get("sources") or []
    sources = {row["source_id"]: row for row in source_rows}
    cited = alert.get("evidence", {}).get("events", [])
    telemetry_ids = [item.get("record_id", item.get("telemetry_id")) for item in cited
                     if item.get("source") == "telemetry"]
    event_ids = [item.get("record_id", item.get("event_id")) for item in cited
                 if item.get("source") == "event"]
    records: dict[tuple[str, int], dict[str, Any]] = {}
    if telemetry_ids:
        cur.execute(
            """SELECT t.telemetry_id AS record_id,t.source_id,t.ts,t.received_at,t.quality,
                      m.name AS series_code
                 FROM v_telemetry_valid t JOIN metric_types m USING (metric_id)
                WHERE t.telemetry_id=ANY(%s)""",
            (telemetry_ids,),
        )
        records.update({("telemetry", row["record_id"]): dict(row) for row in cur.fetchall()})
    if event_ids:
        cur.execute(
            """SELECT e.event_id AS record_id,e.source_id,e.ts,e.received_at,
                      et.name AS series_code
                 FROM event_records e JOIN event_types et USING (event_type_id)
                WHERE e.event_id=ANY(%s)""",
            (event_ids,),
        )
        records.update({("event", row["record_id"]): dict(row) for row in cur.fetchall()})

    rows = []
    uncertain_refs = []
    all_series = summary.get("series") or []
    for item in cited:
        kind = item.get("source")
        record_id = item.get("record_id", item.get("telemetry_id", item.get("event_id")))
        raw = records.get((kind, record_id))
        if raw is None:
            continue
        source = sources.get(raw.get("source_id"), {})
        freshness = next((series for series in all_series
                          if series.get("source_id") == raw.get("source_id")
                          and series.get("record_kind") == kind
                          and ((kind == "telemetry" and series.get("metric_code") == raw.get("series_code"))
                               or (kind == "event" and series.get("event_type") == raw.get("series_code")))), None)
        row = {
            "ref": item.get("ref"),
            "provenance": {
                "source_code": source.get("source_code"),
                "observed_at_utc": raw.get("ts"),
                "received_at_utc": raw.get("received_at"),
            },
            "quality": raw.get("quality") if kind == "telemetry" else None,
            "series_freshness": None if freshness is None else {
                "state": freshness.get("freshness"),
                "age_seconds": freshness.get("age_seconds"),
                "expected_interval_seconds": freshness.get("expected_interval_seconds"),
                "evaluated_at_utc": freshness.get("evaluated_at"),
            },
            "source_liveness": {
                "state": source.get("liveness"),
                "last_received_at_utc": source.get("last_received_at"),
                "evaluated_at_utc": source.get("liveness_evaluated_at"),
            } if source else None,
        }
        rows.append(row)
        if kind == "telemetry" and raw.get("quality") == "UNCERTAIN" and row["ref"]:
            uncertain_refs.append(row["ref"])

    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "state_snapshot": {
            "operational_state": snapshot["operational_state"],
            "availability": snapshot["availability"],
            "communication_status": snapshot["communication_status"],
            "evaluated_at_utc": snapshot["evaluated_at"],
            "state_since_utc": snapshot["state_since"],
            "reason_code": snapshot["reason_code"],
        },
        "series_freshness": [
            {"record_kind": row.get("record_kind"), "metric_code": row.get("metric_code"),
             "event_type": row.get("event_type"), "state": row.get("freshness"),
             "last_observed_at_utc": row.get("last_observed_at"),
             "age_seconds": row.get("age_seconds"),
             "expected_interval_seconds": row.get("expected_interval_seconds"),
             "evaluated_at_utc": row.get("evaluated_at")}
            for row in (summary.get("series") or [])
        ],
        "source_liveness": [
            {"source_code": row.get("source_code"), "state": row.get("liveness"),
             "last_received_at_utc": row.get("last_received_at"),
             "evaluated_at_utc": row.get("liveness_evaluated_at")}
            for row in source_rows
        ],
        "cited_rows": rows,
        "uncertain_evidence_refs": uncertain_refs,
    }
