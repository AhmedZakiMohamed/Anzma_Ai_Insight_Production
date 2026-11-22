#!/usr/bin/env python3
"""Compare G3's operational projection with the existing dashboard bucket.

Run on the seeded database with ``python scripts/state_parity_report.py``.
Add ``--scenarios`` only on a disposable ``anzma_g3_*`` database; synthetic
rows are created inside savepoints and rolled back after each report row.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import re
import sys
from uuid import uuid4

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import psycopg
from psycopg.rows import dict_row

from app.domain.asset_state import OPERATIONAL_BY_BUCKET, derive_asset_state
from app.repositories.dashboard_core_pg import asset_status_rows
from app.services.asset_state import _availability, _dashboard_window


def _emit(row):
    print(json.dumps(row, separators=(",", ":"), default=str))


def _report(conn, asset_id, asset_code, bucket, series, source_ids, now, scenario):
    availability, _availability_evidence = _availability(conn, asset_id)
    derived = derive_asset_state(bucket, series, availability, now, source_ids)
    expected = OPERATIONAL_BY_BUCKET.get(str(bucket).lower(), "UNKNOWN")
    matched = derived["operational_state"] == expected
    result = {
        "event": "STATE_PARITY_ROW", "scenario": scenario, "asset_id": asset_id,
        "asset_code": asset_code, "dashboard_bucket": bucket,
        "derived_operational_state": derived["operational_state"],
        "expected_operational_state": expected, "communication_status": derived["communication_status"],
        "availability": derived["availability"], "source_ids": derived["source_ids"],
        "rule": "dashboard bucket mapping only; alert/event/fault precedence remains in the existing SQL",
        "match": matched,
    }
    _emit(result)
    return matched


def _scenario_asset(conn, scenario, now, system="HVAC"):
    suffix = uuid4().hex[:10]
    building_id = conn.execute(
        "INSERT INTO buildings(name,building_code) VALUES (%s,%s) RETURNING building_id",
        (f"G3 parity {scenario} {suffix}", f"G3P-{suffix}"),
    ).fetchone()["building_id"]
    location_id = conn.execute(
        "INSERT INTO locations(building_id,floor,name) VALUES (%s,0,%s) RETURNING location_id",
        (building_id, f"G3 parity location {suffix}"),
    ).fetchone()["location_id"]
    system_id = conn.execute("SELECT system_type_id FROM system_types WHERE code=%s", (system,)).fetchone()["system_type_id"]
    asset = conn.execute(
        """INSERT INTO assets(asset_code,name,asset_type,system_type_id,location_id)
           VALUES (%s,%s,'g3-parity',%s,%s) RETURNING asset_id""",
        (f"G3P-{scenario}-{suffix}", f"G3 parity {scenario}", system_id, location_id),
    ).fetchone()
    return asset["asset_id"], f"G3P-{scenario}-{suffix}", building_id


def _source(conn, building_id, name, now, state):
    last_received = now - timedelta(hours=2) if state == "SILENT" else now - timedelta(seconds=10)
    row = conn.execute(
        """INSERT INTO data_sources(source_code,building_id,connector_name,connector_version,last_received_at)
           VALUES (%s,%s,'g3-parity','1',%s) RETURNING source_id""",
        (f"G3PS-{name}-{uuid4().hex[:8]}", building_id, last_received),
    ).fetchone()
    source_id = row["source_id"]
    conn.execute(
        """INSERT INTO source_liveness_current
               (source_id,state,evaluated_at,last_received_at,expected_interval_seconds,age_seconds,
                reason_code,reason_message)
           VALUES (%s,%s,%s,%s,300,%s,%s,%s)""",
        (source_id, state, now, last_received, int((now - last_received).total_seconds()),
         "PARITY_SCENARIO", f"synthetic {name} evidence"),
    )
    return source_id


def _series(conn, asset_id, source_id, state, now):
    metric = conn.execute("SELECT metric_id FROM metric_types WHERE name='temperature'").fetchone()["metric_id"]
    exp = conn.execute(
        """INSERT INTO reporting_expectations(scope,asset_id,record_kind,metric_code,
                   expected_interval_seconds,stale_multiplier,missing_multiplier,priority,valid_from)
           VALUES ('ASSET_METRIC',%s,'telemetry','temperature',300,2,5,100,%s)
           RETURNING expectation_id""",
        (asset_id, now - timedelta(days=1)),
    ).fetchone()["expectation_id"]
    observed = now - timedelta(minutes=20)
    conn.execute(
        """INSERT INTO series_freshness_current
               (source_id,asset_id,record_kind,metric_code,freshness,last_observed_at,last_received_at,
                age_seconds,expected_interval_seconds,evaluated_at,expectation_id,expectation_version)
           VALUES (%s,%s,'telemetry','temperature',%s,%s,%s,1200,300,%s,%s,1)""",
        (source_id, asset_id, state, observed, now - timedelta(minutes=19), now, exp),
    )
    return [{"source_id": source_id, "record_kind": "telemetry", "metric_code": "temperature",
             "freshness": state, "last_observed_at": observed}]


def _add_event(conn, asset_id, name, severity, ts):
    event_type = conn.execute("SELECT event_type_id FROM event_types WHERE name=%s", (name,)).fetchone()["event_type_id"]
    conn.execute("INSERT INTO event_records(asset_id,event_type_id,ts,severity) VALUES (%s,%s,%s,%s)",
                 (asset_id, event_type, ts, severity))


def _scenario(conn, name, now):
    conn.execute("SAVEPOINT parity_scenario")
    try:
        system = "CCTV" if name == "fault_open" else "HVAC"
        asset_id, asset_code, building_id = _scenario_asset(conn, name, now, system=system)
        series, source_ids = [], []
        if name == "fault_open":
            _add_event(conn, asset_id, "camera_offline", "critical", now - timedelta(minutes=5))
        elif name == "fault_recovered":
            _add_event(conn, asset_id, "hvac_fault", "critical", now - timedelta(minutes=20))
            _add_event(conn, asset_id, "hvac_recovered", "info", now - timedelta(minutes=10))
        elif name == "resolved_alert":
            fault_ts = now - timedelta(minutes=5)
            _add_event(conn, asset_id, "hvac_fault", "critical", fault_ts)
            conn.execute(
                """INSERT INTO alerts(rule_id,detection_method,severity,asset_id,title,summary,first_ts,last_ts,
                          detected_ts,evidence,status,resolved_ts)
                   VALUES ('HVAC_HIGH_TEMP','threshold','critical',%s,'G3 parity resolved','synthetic',
                           %s,%s,%s,'{}'::jsonb,'resolved',%s)""",
                (asset_id, fault_ts, fault_ts, now, now),
            )
        elif name == "stale_series":
            source_id = _source(conn, building_id, name, now, "ALIVE")
            source_ids.append(source_id)
            series = _series(conn, asset_id, source_id, "STALE", now)
        elif name == "silent_source":
            source_id = _source(conn, building_id, name, now, "SILENT")
            source_ids.append(source_id)
            series = _series(conn, asset_id, source_id, "MISSING", now)
        elif name in ("uncertain_only", "null_legacy_quality"):
            metric_id = conn.execute("SELECT metric_id FROM metric_types WHERE name='temperature'").fetchone()["metric_id"]
            quality = "UNCERTAIN" if name == "uncertain_only" else None
            source_id = _source(conn, building_id, name, now, "ALIVE") if quality else None
            if source_id is not None:
                source_ids.append(source_id)
            conn.execute(
                """INSERT INTO telemetry_records(asset_id,metric_id,ts,value,source_id,received_at,quality)
                   VALUES (%s,%s,%s,24,%s,%s,%s)""",
                (asset_id, metric_id, now - timedelta(minutes=5), source_id,
                 now - timedelta(minutes=4) if source_id else None, quality),
            )
        status_rows = asset_status_rows(conn, *_dashboard_window(conn))
        buckets = {row["asset_id"]: row["status"] for row in status_rows}
        if asset_id not in buckets:
            raise RuntimeError(f"dashboard did not return synthetic asset {asset_id}")
        return _report(conn, asset_id, asset_code, buckets[asset_id], series, source_ids, now, name)
    finally:
        conn.execute("ROLLBACK TO SAVEPOINT parity_scenario")
        conn.execute("RELEASE SAVEPOINT parity_scenario")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.getenv("PGDATABASE"), help="target database (defaults to PGDATABASE)")
    parser.add_argument("--scenarios", action="store_true", help="run synthetic savepoint/rollback scenarios")
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("--db or PGDATABASE is required")
    if args.scenarios and not re.fullmatch(r"anzma_g3_[a-z0-9_]+", args.db):
        parser.error("--scenarios requires a disposable database named anzma_g3_*")
    conn = psycopg.connect(host=os.environ["PGHOST"], port=int(os.environ["PGPORT"]), dbname=args.db,
                           user=os.environ["PGUSER"], password=os.getenv("PGPASSWORD", ""), row_factory=dict_row)
    try:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        start, end = _dashboard_window(conn)
        bucket_rows = asset_status_rows(conn, start, end)
        codes = {row["asset_id"]: row["asset_code"] for row in conn.execute("SELECT asset_id,asset_code FROM assets")}
        mismatches = 0
        for row in bucket_rows:
            if not _report(conn, row["asset_id"], codes[row["asset_id"]], row["status"], [], [], now, "seeded"):
                mismatches += 1
        scenario_names = ("fault_open", "fault_recovered", "resolved_alert", "stale_series",
                          "silent_source", "uncertain_only", "null_legacy_quality")
        if args.scenarios:
            for name in scenario_names:
                conn.execute("SAVEPOINT parity_scenario_transaction")
                try:
                    # Each scenario's internal savepoint rollback is independent and leaves no fact rows behind.
                    if not _scenario(conn, name, now):
                        mismatches += 1
                finally:
                    conn.execute("ROLLBACK TO SAVEPOINT parity_scenario_transaction")
                    conn.execute("RELEASE SAVEPOINT parity_scenario_transaction")
        _emit({"event": "STATE_PARITY_SUMMARY", "database": args.db,
               "seeded_assets": len(bucket_rows), "scenarios": list(scenario_names) if args.scenarios else [],
               "mismatches": mismatches})
        conn.rollback()
        return 1 if mismatches else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
