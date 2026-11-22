"""Freshness persistence tests; requires a disposable anzma_e3_* PostgreSQL database."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg

from app.adapters.database import db_adapter as da
from app.services.freshness import run_once


ROOT = Path(__file__).resolve().parents[2]
DATABASE = os.environ.get("ANZMA_E3_TEST_DATABASE", "")
# Later than migration-created defaults, including on runs after this test was authored.
NOW = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=5)


def test_db():
    if not DATABASE.startswith("anzma_e3_"):
        raise RuntimeError("ANZMA_E3_TEST_DATABASE must name a disposable anzma_e3_* database")
    host = os.environ.get("PGHOST")
    port = os.environ.get("PGPORT")
    if not host or not port:
        raise RuntimeError("PGHOST and PGPORT must explicitly target the disposable PostgreSQL")
    return da.connect(host=host, port=port, dbname=DATABASE,
                      user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""))


class FreshnessJobIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATABASE.startswith("anzma_e3_"):
            raise RuntimeError("ANZMA_E3_TEST_DATABASE must name a disposable anzma_e3_* database")
        if os.environ.get("PGHOST") != "127.0.0.1" or os.environ.get("PGPORT") != "25443":
            raise RuntimeError("set PGHOST=127.0.0.1 and PGPORT=25443 for the E3 disposable container")
        cls.conn = psycopg.connect(
            host="127.0.0.1", port=25443, dbname=DATABASE,
            user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""),
        )
        cls.location_id, cls.system_type_id, cls.building_id = cls.conn.execute(
            """SELECT l.location_id,a.system_type_id,l.building_id
                 FROM assets a JOIN locations l USING(location_id) ORDER BY a.asset_id LIMIT 1"""
        ).fetchone()
        cls.metric_id, cls.metric_code = cls.conn.execute(
            "SELECT metric_id,name FROM metric_types WHERE name='temperature'"
        ).fetchone()
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        self.conn.execute("""TRUNCATE data_gap_cause_history,source_liveness_transitions,source_liveness_current,
                              series_freshness_current,data_gaps RESTART IDENTITY""")
        self.conn.commit()

    def add_asset(self, tag: str) -> int:
        suffix = uuid4().hex[:12]
        return self.conn.execute(
            """INSERT INTO assets(asset_code,name,asset_type,system_type_id,location_id)
               VALUES (%s,%s,'ahu',%s,%s) RETURNING asset_id""",
            (f"E3-{tag}-{suffix}", f"E3 {tag} {suffix}", self.system_type_id, self.location_id),
        ).fetchone()[0]

    def add_expectation(self, asset_id: int, kind="telemetry", interval=100,
                        metric_code=None, event_type=None):
        metric_code = self.metric_code if kind == "telemetry" and metric_code is None else metric_code
        return self.conn.execute(
            """INSERT INTO reporting_expectations
                   (scope,asset_id,record_kind,metric_code,event_type,expected_interval_seconds,
                    stale_multiplier,missing_multiplier,priority,valid_from)
               VALUES ('ASSET_METRIC',%s,%s,%s,%s,%s,2,5,71,%s) RETURNING expectation_id""",
            (asset_id, kind, metric_code, event_type, interval, NOW - timedelta(days=1)),
        ).fetchone()[0]

    def add_source(self, tag: str, asset_id: int) -> int:
        return self.conn.execute(
            """INSERT INTO data_sources(source_code,building_id,connector_name,connector_version)
               VALUES (%s,%s,'e3-test','1') RETURNING source_id""",
            (f"E3-{tag}-{uuid4().hex[:10]}", self.building_id),
        ).fetchone()[0]

    def add_batch(self, source_id: int, mode: str, received_at: datetime, accepted=1, rejected=0) -> int:
        return self.conn.execute(
            """INSERT INTO ingestion_batches
                   (source_id,batch_id,contract_version,payload_sha256,received_at,sent_at,
                    connector_name,connector_version,received_count,accepted_count,duplicate_count,
                    rejected_count,response_payload,delivery_mode)
               VALUES (%s,%s,'v1',%s,%s,%s,'e3-test','1',%s,%s,0,%s,'{}'::jsonb,%s)
               RETURNING ingestion_batch_id""",
            (source_id, uuid4(), hashlib.sha256(uuid4().bytes).hexdigest(), received_at,
             received_at - timedelta(seconds=1), accepted + rejected, accepted, rejected, mode),
        ).fetchone()[0]

    def add_rejection(self, batch_id: int, source_id: int, payload: dict):
        self.conn.execute(
            """INSERT INTO ingestion_rejections
                   (ingestion_batch_id,record_index,record_type,reason_code,message,payload,source_id)
               VALUES (%s,0,'telemetry','SCHEMA_INVALID','rejected test record',%s::jsonb,%s)""",
            (batch_id, json.dumps(payload), source_id),
        )
        self.conn.commit()

    def add_telemetry(self, asset_id: int, observed_at: datetime, received_at: datetime,
                      quality="GOOD", source_id=None, batch_id=None):
        self.conn.execute(
            """INSERT INTO telemetry_records
                   (asset_id,metric_id,ts,value,source_id,received_at,quality,ingestion_batch_id,contract_version)
               VALUES (%s,%s,%s,23.5,%s,%s,%s,%s,'v1')""",
            (asset_id, self.metric_id, observed_at, source_id, received_at, quality, batch_id),
        )
        self.conn.commit()

    def run_evaluator(self, now=NOW, batch_size=250):
        conn = test_db()
        try:
            return run_once(conn, now, batch_size=batch_size)
        finally:
            conn.close()

    def current_row(self, asset_id: int, kind="telemetry", metric_code=None, event_type=None):
        if kind == "telemetry" and metric_code is None:
            metric_code = self.metric_code
        return self.conn.execute(
            """SELECT source_id,freshness,last_observed_at,last_received_at,age_seconds,expectation_id
                 FROM series_freshness_current WHERE asset_id=%s AND record_kind=%s
                   AND metric_code IS NOT DISTINCT FROM %s AND event_type IS NOT DISTINCT FROM %s
                 ORDER BY source_id NULLS FIRST LIMIT 1""",
            (asset_id, kind, metric_code, event_type),
        ).fetchone()

    def test_unknown_and_not_applicable_are_distinct_and_heartbeat_can_appear_later(self):
        expected_asset = self.add_asset("UNKNOWN")
        self.add_expectation(expected_asset)
        event_only = self.add_asset("EVENT")
        door_forced = self.conn.execute("SELECT event_type_id FROM event_types WHERE name='door_forced'").fetchone()[0]
        self.conn.execute(
            "INSERT INTO event_records(asset_id,event_type_id,ts,severity,received_at) VALUES (%s,%s,%s,'info',%s)",
            (event_only, door_forced, NOW - timedelta(seconds=20), NOW),
        )
        self.conn.commit()

        self.run_evaluator()
        self.assertEqual(self.current_row(expected_asset)[1], "UNKNOWN")
        self.assertIsNone(self.current_row(event_only, "event", event_type="door_forced"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM data_gaps WHERE asset_id IN (%s,%s)",
                                           (expected_asset, event_only)).fetchone()[0], 0)

        self.add_expectation(event_only, kind="event", event_type="heartbeat")
        self.conn.commit()
        self.run_evaluator()
        self.assertEqual(self.current_row(event_only, "event", event_type="heartbeat")[1], "UNKNOWN")
        heartbeat_id = self.conn.execute("SELECT event_type_id FROM event_types WHERE name='heartbeat'").fetchone()[0]
        self.conn.execute(
            "INSERT INTO event_records(asset_id,event_type_id,ts,severity,received_at) VALUES (%s,%s,%s,'info',%s)",
            (event_only, heartbeat_id, NOW - timedelta(seconds=10), NOW),
        )
        self.conn.commit()
        self.run_evaluator()
        self.assertEqual(self.current_row(event_only, "event", event_type="heartbeat")[1], "FRESH")

    def test_missing_gap_is_idempotent_backfill_does_not_close_and_newer_observation_closes(self):
        asset = self.add_asset("GAP")
        self.add_expectation(asset)
        first = NOW - timedelta(seconds=1000)
        self.add_telemetry(asset, first, NOW - timedelta(seconds=900))
        result = self.run_evaluator()
        self.assertGreaterEqual(result["gaps_opened"], 1)
        original = self.conn.execute("SELECT gap_id,gap_start,gap_end FROM data_gaps WHERE asset_id=%s", (asset,)).fetchone()
        self.assertEqual((original[1], original[2]), (first, None))

        self.run_evaluator()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM data_gaps WHERE asset_id=%s", (asset,)).fetchone()[0], 1)
        source_id = self.add_source("REJECTED", asset)
        rejected_batch = self.add_batch(source_id, "LIVE", NOW, accepted=0, rejected=1)
        self.add_rejection(rejected_batch, source_id,
                           {"asset_id": asset, "observed_at": (NOW + timedelta(hours=1)).isoformat()})
        self.run_evaluator()
        self.assertIsNone(self.conn.execute("SELECT gap_end FROM data_gaps WHERE gap_id=%s", (original[0],)).fetchone()[0])
        self.add_telemetry(asset, first - timedelta(seconds=100), NOW)
        self.run_evaluator()
        self.assertIsNone(self.conn.execute("SELECT gap_end FROM data_gaps WHERE gap_id=%s", (original[0],)).fetchone()[0])

        late = first + timedelta(seconds=100)
        self.add_telemetry(asset, late, NOW)
        self.run_evaluator()
        self.assertIsNone(self.conn.execute("SELECT gap_end FROM data_gaps WHERE gap_id=%s", (original[0],)).fetchone()[0])
        self.assertEqual(self.conn.execute("SELECT gap_start FROM data_gaps WHERE gap_id=%s", (original[0],)).fetchone()[0], first)

        resumed = NOW + timedelta(seconds=10)
        self.add_telemetry(asset, resumed, NOW + timedelta(seconds=11))
        self.run_evaluator(now=NOW + timedelta(seconds=20))
        closed = self.conn.execute("SELECT gap_end,closed_by,closed_at FROM data_gaps WHERE gap_id=%s", (original[0],)).fetchone()
        self.assertEqual(closed, (resumed, "OBSERVATION", NOW + timedelta(seconds=20)))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM data_gaps WHERE asset_id=%s AND gap_end IS NULL",
                                           (asset,)).fetchone()[0], 0)

        newest = NOW + timedelta(seconds=15)
        self.add_telemetry(asset, newest, NOW + timedelta(seconds=21))
        self.run_evaluator(now=NOW + timedelta(seconds=22))
        self.assertEqual(self.current_row(asset)[1], "FRESH")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM data_gaps WHERE asset_id=%s AND gap_end IS NULL",
                                           (asset,)).fetchone()[0], 0)

    def test_age_uses_max_observed_excludes_bad_and_ignores_backfill_arrival_time(self):
        asset = self.add_asset("BACKFILL")
        self.add_expectation(asset)
        source_id = self.add_source("BACKFILL", asset)
        new_observation = NOW - timedelta(seconds=250)
        live_batch = self.add_batch(source_id, "LIVE", NOW - timedelta(seconds=240))
        self.add_telemetry(asset, new_observation, NOW - timedelta(seconds=240), source_id=source_id,
                           batch_id=live_batch)
        old_observation = NOW - timedelta(seconds=1500)
        backfill_batch = self.add_batch(source_id, "BACKFILL", NOW - timedelta(seconds=1))
        self.add_telemetry(asset, old_observation, NOW - timedelta(seconds=1), source_id=source_id,
                           batch_id=backfill_batch)
        self.add_telemetry(asset, NOW - timedelta(seconds=10), NOW, quality="BAD", source_id=source_id)

        self.run_evaluator()
        row = self.current_row(asset)
        self.assertEqual(row[0], source_id)
        self.assertEqual(row[1], "STALE")
        self.assertEqual(row[2], new_observation)
        self.assertEqual(row[3], NOW - timedelta(seconds=1))
        self.assertEqual(row[4], 250)
        self.assertEqual(self.conn.execute(
            "SELECT max(ts) FROM v_telemetry_valid WHERE asset_id=%s AND source_id=%s",
            (asset, source_id),
        ).fetchone()[0], new_observation)

    def test_legacy_null_source_rows_are_persisted_with_null_identity(self):
        seed = self.conn.execute(
            """SELECT t.asset_id,m.name FROM v_telemetry_valid t JOIN metric_types m USING(metric_id)
                 WHERE t.source_id IS NULL ORDER BY t.asset_id LIMIT 1"""
        ).fetchone()
        self.assertIsNotNone(seed)
        self.run_evaluator()
        self.assertIsNotNone(self.current_row(seed[0], metric_code=seed[1]))
        self.assertIsNone(self.current_row(seed[0], metric_code=seed[1])[0])

    def test_same_clock_rerun_does_not_change_current_rows_or_gaps(self):
        self.run_evaluator(batch_size=5)
        before_current = self.conn.execute(
            """SELECT xmin::text,source_id,asset_id,record_kind,metric_code,event_type,freshness,last_observed_at,
                      last_received_at,age_seconds,expected_interval_seconds,evaluated_at,expectation_id,expectation_version
                 FROM series_freshness_current ORDER BY series_freshness_id"""
        ).fetchall()
        before_gaps = self.conn.execute(
            """SELECT xmin::text,gap_id,source_id,asset_id,record_kind,metric_code,event_type,gap_start,gap_end,closed_by,
                      opened_at,last_evaluated_at,closed_at FROM data_gaps ORDER BY gap_id"""
        ).fetchall()
        self.conn.commit()
        self.run_evaluator(batch_size=5)
        after_current = self.conn.execute(
            """SELECT xmin::text,source_id,asset_id,record_kind,metric_code,event_type,freshness,last_observed_at,
                      last_received_at,age_seconds,expected_interval_seconds,evaluated_at,expectation_id,expectation_version
                 FROM series_freshness_current ORDER BY series_freshness_id"""
        ).fetchall()
        after_gaps = self.conn.execute(
            """SELECT xmin::text,gap_id,source_id,asset_id,record_kind,metric_code,event_type,gap_start,gap_end,closed_by,
                      opened_at,last_evaluated_at,closed_at FROM data_gaps ORDER BY gap_id"""
        ).fetchall()
        self.assertEqual(after_current, before_current)
        self.assertEqual(after_gaps, before_gaps)
        self.conn.commit()

    def test_failed_page_rolls_back_while_prior_pages_remain_committed(self):
        asset = self.add_asset("ROLLBACK")
        self.add_expectation(asset)
        self.conn.commit()
        function = "e3_test_fail_freshness_page"
        trigger = "e3_test_fail_freshness_page_trigger"
        self.conn.execute(f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                          f"IF NEW.asset_id={int(asset)} THEN RAISE EXCEPTION 'injected E3 page failure'; END IF; "
                          "RETURN NEW; END $$")
        self.conn.execute(f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE ON series_freshness_current "
                          f"FOR EACH ROW EXECUTE FUNCTION {function}()")
        self.conn.commit()
        try:
            with self.assertRaisesRegex(RuntimeError, "page .* failed") as caught:
                self.run_evaluator(batch_size=1)
            self.assertLessEqual(len(str(caught.exception)), 500)
            self.assertGreater(self.conn.execute("SELECT count(*) FROM series_freshness_current").fetchone()[0], 0)
            self.assertIsNone(self.current_row(asset, metric_code=self.metric_code))
            self.conn.commit()
        finally:
            self.conn.execute(f"DROP TRIGGER IF EXISTS {trigger} ON series_freshness_current")
            self.conn.execute(f"DROP FUNCTION IF EXISTS {function}()")
            self.conn.commit()

    def test_once_entry_point_emits_real_json_summary(self):
        env = os.environ.copy()
        env.update(PGHOST="127.0.0.1", PGPORT="25443", PGDATABASE=DATABASE)
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/freshness_job.py"), "--once"],
            cwd=ROOT, env=env, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual([record["event"] for record in records],
                         ["FRESHNESS_LOCK_ACQUIRED", "FRESHNESS_PASS", "FRESHNESS_LOCK_RELEASED"])
        summary = records[1]
        self.assertEqual(summary["event"], "FRESHNESS_PASS")
        self.assertGreater(summary["series_evaluated"], 0)
        self.assertEqual(summary["series_evaluated"], sum(summary["states"].values()))
        self.assertEqual(set(summary["states"]), {"FRESH", "STALE", "MISSING", "UNKNOWN", "NOT_APPLICABLE"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
