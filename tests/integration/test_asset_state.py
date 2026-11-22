"""G3 persistence checks; each test clones and drops its own disposable database."""
from __future__ import annotations

import os
import re
import threading
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import patch

import psycopg
from psycopg.rows import dict_row

from app.repositories.dashboard_core_pg import asset_status_rows
from app.services.asset_state import alert_evidence_context, run_once


HOST = os.getenv("PGHOST", "")
PORT = os.getenv("PGPORT", "")
USER = os.getenv("PGUSER", "")
PASSWORD = os.getenv("PGPASSWORD", "")
TEMPLATE = os.getenv("ANZMA_G3_TEMPLATE_DATABASE", "")


class AssetStatePersistenceTests(unittest.TestCase):
    def setUp(self):
        if HOST != "127.0.0.1" or PORT != "25443":
            self.skipTest("G3 integration tests require the API-container disposable DB proxy on 127.0.0.1:25443")
        if not re.fullmatch(r"anzma_g3_[a-z0-9_]+", TEMPLATE):
            self.skipTest("set ANZMA_G3_TEMPLATE_DATABASE to a dedicated anzma_g3_* template")
        self.database = f"anzma_g3_case_{uuid4().hex[:12]}"
        self.admin = psycopg.connect(host=HOST, port=PORT, dbname="postgres", user=USER,
                                     password=PASSWORD, autocommit=True)
        self.admin.execute(f'CREATE DATABASE "{self.database}" TEMPLATE "{TEMPLATE}"')
        self.conn = psycopg.connect(host=HOST, port=PORT, dbname=self.database, user=USER,
                                    password=PASSWORD, row_factory=dict_row)
        self.addCleanup(self._drop_test_database)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        suffix = uuid4().hex[:10]
        self.building_id = self.conn.execute(
            "INSERT INTO buildings(name,building_code) VALUES (%s,%s) RETURNING building_id",
            (f"G3 test {suffix}", f"G3-{suffix}"),
        ).fetchone()["building_id"]
        self.location_id = self.conn.execute(
            "INSERT INTO locations(building_id,floor,name) VALUES (%s,0,%s) RETURNING location_id",
            (self.building_id, f"G3 test location {suffix}"),
        ).fetchone()["location_id"]
        self.metric_id = self.conn.execute(
            "SELECT metric_id FROM metric_types WHERE name='temperature'"
        ).fetchone()["metric_id"]
        self.system_id = self.conn.execute(
            "SELECT system_type_id FROM system_types WHERE code='HVAC'"
        ).fetchone()["system_type_id"]
        self.asset_id = self.add_asset(suffix)
        self.source_id = self.add_source(suffix)
        self.expectation_id = self.add_expectation()
        self.conn.commit()

    def _drop_test_database(self):
        try:
            if hasattr(self, "conn"):
                self.conn.close()
            if hasattr(self, "admin"):
                self.admin.execute(f'DROP DATABASE IF EXISTS "{self.database}" WITH (FORCE)')
                self.admin.close()
        except Exception:
            if hasattr(self, "admin"):
                self.admin.close()

    def add_asset(self, suffix: str) -> int:
        return self.conn.execute(
            """INSERT INTO assets(asset_code,name,asset_type,system_type_id,location_id)
               VALUES (%s,%s,'ahu',%s,%s) RETURNING asset_id""",
            (f"G3-{suffix}", f"G3 test asset {suffix}", self.system_id, self.location_id),
        ).fetchone()["asset_id"]

    def add_source(self, suffix: str) -> int:
        return self.conn.execute(
            """INSERT INTO data_sources(source_code,building_id,connector_name,connector_version)
               VALUES (%s,%s,'g3-test','1') RETURNING source_id""",
            (f"G3SRC-{suffix}", self.building_id),
        ).fetchone()["source_id"]

    def add_expectation(self) -> int:
        return self.conn.execute(
            """INSERT INTO reporting_expectations(scope,asset_id,record_kind,metric_code,
                       expected_interval_seconds,stale_multiplier,missing_multiplier,priority,valid_from)
               VALUES ('ASSET_METRIC',%s,'telemetry','temperature',300,2,5,100,%s)
               RETURNING expectation_id""",
            (self.asset_id, self.now - timedelta(days=1)),
        ).fetchone()["expectation_id"]

    def add_series(self, source_id: int, freshness: str, metric="temperature"):
        metric_id = self.conn.execute("SELECT metric_id FROM metric_types WHERE name=%s", (metric,)).fetchone()["metric_id"]
        observed = None if freshness == "UNKNOWN" else self.now - timedelta(minutes=10)
        self.conn.execute(
            """INSERT INTO series_freshness_current
                   (source_id,asset_id,record_kind,metric_code,freshness,last_observed_at,last_received_at,
                    age_seconds,expected_interval_seconds,evaluated_at,expectation_id,expectation_version)
               VALUES (%s,%s,'telemetry',%s,%s,%s,%s,600,300,%s,%s,1)""",
            (source_id, self.asset_id, metric, freshness, observed, self.now - timedelta(minutes=9),
             self.now, self.expectation_id),
        )

    def current(self):
        return self.conn.execute(
            "SELECT operational_state,availability,communication_status,evaluated_at,state_since,evidence_summary,"
            "evidence_schema_version "
            "FROM asset_state_current WHERE asset_id=%s", (self.asset_id,),
        ).fetchone()

    def test_baseline_replay_and_semantic_transition(self):
        self.add_series(self.source_id, "MISSING")
        self.conn.commit()
        run_once(self.conn, self.now)
        row = self.current()
        self.assertEqual(row["communication_status"], "SILENT")
        self.assertEqual(row["availability"], "UNKNOWN")
        self.assertEqual(row["evidence_schema_version"], 1)
        self.assertNotIn(row["communication_status"], {"OFFLINE", "UNAVAILABLE"})
        self.assertEqual(self.conn.execute(
            "SELECT kind FROM asset_state_history WHERE asset_id=%s", (self.asset_id,)
        ).fetchall(), [{"kind": "BASELINE"}])
        run_once(self.conn, self.now + timedelta(seconds=1))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM asset_state_history WHERE asset_id=%s", (self.asset_id,)
        ).fetchone()["n"], 1)

        event_type_id = self.conn.execute("SELECT event_type_id FROM event_types WHERE name='hvac_fault'").fetchone()["event_type_id"]
        self.conn.execute(
            "INSERT INTO event_records(asset_id,event_type_id,ts,severity) VALUES (%s,%s,%s,'critical')",
            (self.asset_id, event_type_id, self.now),
        )
        self.conn.commit()
        self.assertEqual({row["asset_id"]: row["status"] for row in
                          asset_status_rows(self.conn, self.now - timedelta(hours=24),
                                            self.now + timedelta(seconds=1))}[self.asset_id], "critical")
        run_once(self.conn, self.now + timedelta(seconds=2))
        self.assertEqual(self.current()["operational_state"], "CRITICAL")
        self.assertEqual(self.conn.execute(
            "SELECT kind FROM asset_state_history WHERE asset_id=%s ORDER BY asset_state_history_id",
            (self.asset_id,),
        ).fetchall(), [{"kind": "BASELINE"}, {"kind": "TRANSITION"}])

    def test_history_rows_reject_update_and_delete(self):
        self.add_series(self.source_id, "FRESH")
        self.conn.commit()
        run_once(self.conn, self.now)
        history_id = self.conn.execute(
            "SELECT asset_state_history_id FROM asset_state_history WHERE asset_id=%s",
            (self.asset_id,),
        ).fetchone()["asset_state_history_id"]
        for statement in (
            "UPDATE asset_state_history SET reason_message='changed' WHERE asset_state_history_id=%s",
            "DELETE FROM asset_state_history WHERE asset_state_history_id=%s",
        ):
            with self.assertRaises(psycopg.errors.RaiseException):
                self.conn.execute(statement, (history_id,))
            self.conn.rollback()

    def test_multisource_is_unknown_and_connector_availability_is_only_evidence(self):
        self.add_series(self.source_id, "FRESH")
        second_source = self.add_source(uuid4().hex[:10])
        self.add_series(second_source, "FRESH", metric="humidity")
        self.conn.commit()
        run_once(self.conn, self.now)
        self.assertEqual(self.current()["communication_status"], "UNKNOWN")
        self.assertEqual(self.current()["evidence_summary"]["source_ids"],
                         sorted([self.source_id, second_source]))
        self.assertTrue(self.current()["evidence_summary"]["attribution_ambiguous"])

    def test_core_availability_events_only_and_quality_provenance(self):
        self.add_series(self.source_id, "STALE")
        self.conn.execute(
            """INSERT INTO source_liveness_current
                   (source_id,state,evaluated_at,last_received_at,expected_interval_seconds,age_seconds,reason_code,
                    reason_message,active_series_count,stale_series_count,missing_series_count,mass_stale)
               VALUES (%s,'ALIVE',%s,%s,300,0,'RECEIVED','source recently received a batch',1,0,0,false)""",
            (self.source_id, self.now, self.now),
        )
        # A source-reported availability is not used to set Core availability.
        state_batch = self.conn.execute(
            """INSERT INTO ingestion_batches
                   (source_id,batch_id,contract_version,payload_sha256,received_at,sent_at,
                    connector_name,connector_version,received_count,accepted_count,duplicate_count,
                    rejected_count,response_payload)
               VALUES (%s,%s,'v1',repeat('a',64),%s,%s,'g3-test','1',1,1,0,0,'{}'::jsonb)
               RETURNING ingestion_batch_id""",
            (self.source_id, uuid4(), self.now, self.now),
        ).fetchone()["ingestion_batch_id"]
        self.conn.execute(
            """INSERT INTO equipment_state_records(source_id,asset_id,observed_at,received_at,
                       reported_state,availability,contract_version,ingestion_batch_id)
               VALUES (%s,%s,%s,%s,'UNKNOWN','UNAVAILABLE','v1',%s)""",
            (self.source_id, self.asset_id, self.now, self.now, state_batch),
        )
        self.conn.execute(
            """INSERT INTO telemetry_records(asset_id,metric_id,ts,value,source_id,received_at,quality)
               VALUES (%s,%s,%s,25,%s,%s,'UNCERTAIN'),(%s,%s,%s,26,%s,%s,'BAD'),
                      (%s,%s,%s,45,NULL,NULL,NULL)""",
            (self.asset_id, self.metric_id, self.now - timedelta(seconds=3), self.source_id, self.now,
             self.asset_id, self.metric_id, self.now - timedelta(seconds=2), self.source_id, self.now,
             self.asset_id, self.conn.execute("SELECT metric_id FROM metric_types WHERE name='humidity'").fetchone()["metric_id"],
             self.now - timedelta(seconds=1)),
        )
        self.conn.commit()
        run_once(self.conn, self.now)
        row = self.current()
        self.assertEqual(row["availability"], "UNKNOWN")
        evidence = row["evidence_summary"]
        uncertain_id = self.conn.execute(
            "SELECT telemetry_id FROM telemetry_records WHERE asset_id=%s AND quality='UNCERTAIN'", (self.asset_id,)
        ).fetchone()["telemetry_id"]
        bad_id = self.conn.execute(
            "SELECT telemetry_id FROM telemetry_records WHERE asset_id=%s AND quality='BAD'", (self.asset_id,)
        ).fetchone()["telemetry_id"]
        legacy_id = self.conn.execute(
            "SELECT telemetry_id FROM telemetry_records WHERE asset_id=%s AND metric_id=(SELECT metric_id FROM metric_types WHERE name='humidity')",
            (self.asset_id,),
        ).fetchone()["telemetry_id"]
        self.assertIn(f"telemetry:{uncertain_id}", evidence["uncertain_refs"])
        refs = {f"telemetry:{item['telemetry_id']}" for item in evidence["measurements"]}
        self.assertNotIn(f"telemetry:{bad_id}", refs)
        self.assertIn(f"telemetry:{legacy_id}", evidence["legacy_or_unattributed_refs"])
        context = alert_evidence_context(self.conn.cursor(), {
            "asset_id": self.asset_id,
            "evidence": {"events": [{"ref": "E1", "source": "telemetry", "record_id": uncertain_id}]},
        })
        self.assertEqual(context["uncertain_evidence_refs"], ["E1"])
        self.assertEqual(context["cited_rows"][0]["quality"], "UNCERTAIN")
        self.assertEqual(context["cited_rows"][0]["source_liveness"]["state"], "ALIVE")
        self.assertEqual(context["cited_rows"][0]["series_freshness"]["state"], "STALE")
        self.assertEqual(context["cited_rows"][0]["provenance"]["source_code"],
                         self.conn.execute("SELECT source_code FROM data_sources WHERE source_id=%s",
                                           (self.source_id,)).fetchone()["source_code"])

        event_type_id = self.conn.execute("SELECT event_type_id FROM event_types WHERE name='camera_offline'").fetchone()["event_type_id"]
        cctv_id = self.conn.execute("SELECT system_type_id FROM system_types WHERE code='CCTV'").fetchone()["system_type_id"]
        self.conn.execute("UPDATE assets SET system_type_id=%s WHERE asset_id=%s", (cctv_id, self.asset_id))
        self.conn.execute(
            "INSERT INTO event_records(asset_id,event_type_id,ts,severity) VALUES (%s,%s,%s,'warning')",
            (self.asset_id, event_type_id, self.now),
        )
        self.conn.commit()
        run_once(self.conn, self.now + timedelta(seconds=1))
        self.assertEqual(self.current()["availability"], "UNAVAILABLE")

    def test_failed_evaluation_preserves_last_committed_snapshot(self):
        self.add_series(self.source_id, "FRESH")
        self.conn.commit()
        run_once(self.conn, self.now)
        before = self.current()
        with patch("app.services.asset_state.derive_asset_state", side_effect=RuntimeError("injected failure")):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                run_once(self.conn, self.now + timedelta(seconds=1))
        self.assertEqual(self.current(), before)

    def test_concurrent_replays_write_one_baseline(self):
        self.add_series(self.source_id, "FRESH")
        self.conn.commit()
        database = self.database
        errors = []
        start = threading.Barrier(2)

        def evaluate():
            conn = psycopg.connect(host=HOST, port=PORT, dbname=database, user=USER,
                                   password=PASSWORD, row_factory=dict_row)
            try:
                start.wait()
                run_once(conn, self.now)
            except Exception as exc:  # surfaced as a test assertion below
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=evaluate) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads), "concurrent evaluator hung")
        self.assertEqual(errors, [])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM asset_state_history WHERE asset_id=%s AND kind='BASELINE'",
            (self.asset_id,),
        ).fetchone()["n"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
