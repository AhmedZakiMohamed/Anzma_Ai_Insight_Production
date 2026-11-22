"""E4b source liveness and gap-cause tests on disposable anzma_e4b_* PostgreSQL."""
from __future__ import annotations

import hashlib
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg

from app.adapters.database import db_adapter as da
from app.services.freshness import run_once
from app.config.source_liveness import SourceLivenessConfig


DATABASE = os.environ.get("ANZMA_E4B_TEST_DATABASE", "")
NOW = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=5)


class SourceLivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATABASE.startswith("anzma_e4b_"):
            raise RuntimeError("ANZMA_E4B_TEST_DATABASE must name a disposable anzma_e4b_* database")
        if os.environ.get("PGHOST") != "127.0.0.1" or os.environ.get("PGPORT") != "25443":
            raise RuntimeError("set PGHOST=127.0.0.1 and PGPORT=25443 for the disposable database")
        cls.db = psycopg.connect(host="127.0.0.1", port=25443, dbname=DATABASE,
                                 user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""))
        cls.metric_id = cls.db.execute("SELECT metric_id FROM metric_types WHERE name='temperature'").fetchone()[0]
        cls.system_type_id = cls.db.execute("SELECT system_type_id FROM system_types WHERE code='HVAC'").fetchone()[0]
        cls.db.commit()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def make_world(self, groups: tuple[int, ...] = (1,)) -> list[dict]:
        suffix = uuid4().hex[:10]
        building = self.db.execute("INSERT INTO buildings(name,building_code) VALUES (%s,%s) RETURNING building_id",
                                   (f"E4B-{suffix}", f"E4B-BLD-{suffix}")).fetchone()[0]
        location = self.db.execute("INSERT INTO locations(building_id,floor,name) VALUES (%s,0,%s) RETURNING location_id",
                                   (building, f"E4B-{suffix}")).fetchone()[0]
        worlds = []
        for group_idx, count in enumerate(groups):
            source = self.db.execute(
                """INSERT INTO data_sources(source_code,building_id,connector_name,connector_version,
                          keepalive_interval_seconds)
                   VALUES (%s,%s,'e4b-test','1',100) RETURNING source_id""",
                (f"E4B-{suffix}-{group_idx}", building),
            ).fetchone()[0]
            assets = []
            for index in range(count):
                asset = self.db.execute(
                    """INSERT INTO assets(asset_code,name,asset_type,system_type_id,location_id)
                       VALUES (%s,%s,'ahu',%s,%s) RETURNING asset_id""",
                    (f"E4B-{suffix}-{group_idx}-{index}", f"E4B asset {index}", self.system_type_id, location),
                ).fetchone()[0]
                expectation = self.db.execute(
                    """INSERT INTO reporting_expectations(scope,asset_id,record_kind,metric_code,
                               expected_interval_seconds,stale_multiplier,missing_multiplier,priority,valid_from)
                       VALUES ('ASSET_METRIC',%s,'telemetry','temperature',100,2,5,71,%s)
                       RETURNING expectation_id""",
                    (asset, NOW - timedelta(days=1)),
                ).fetchone()[0]
                assets.append({"asset": asset, "expectation": expectation})
            worlds.append({"source": source, "assets": assets})
        self.db.commit()
        self.addCleanup(self.cleanup_world, worlds, building, location)
        return worlds

    def cleanup_world(self, worlds, building, location):
        sources = [world["source"] for world in worlds]
        assets = [item["asset"] for world in worlds for item in world["assets"]]
        if not sources:
            return
        self.db.execute("DELETE FROM data_gap_cause_history WHERE gap_id IN (SELECT gap_id FROM data_gaps WHERE asset_id=ANY(%s))",
                        (assets,))
        self.db.execute("DELETE FROM data_gaps WHERE asset_id=ANY(%s)", (assets,))
        self.db.execute("DELETE FROM series_freshness_current WHERE asset_id=ANY(%s)", (assets,))
        self.db.execute("DELETE FROM telemetry_records WHERE asset_id=ANY(%s)", (assets,))
        self.db.execute("""DELETE FROM ingestion_rejections WHERE ingestion_batch_id IN
                           (SELECT ingestion_batch_id FROM ingestion_batches WHERE source_id=ANY(%s))""", (sources,))
        self.db.execute("DELETE FROM ingestion_batches WHERE source_id=ANY(%s)", (sources,))
        self.db.execute("DELETE FROM source_liveness_transitions WHERE source_id=ANY(%s)", (sources,))
        self.db.execute("DELETE FROM source_liveness_current WHERE source_id=ANY(%s)", (sources,))
        self.db.execute("DELETE FROM reporting_expectations WHERE expectation_id=ANY(%s)",
                         ([item["expectation"] for world in worlds for item in world["assets"]],))
        self.db.execute("DELETE FROM data_sources WHERE source_id=ANY(%s)", (sources,))
        self.db.execute("DELETE FROM assets WHERE asset_id=ANY(%s)", (assets,))
        self.db.execute("DELETE FROM locations WHERE location_id=%s", (location,))
        self.db.execute("DELETE FROM buildings WHERE building_id=%s", (building,))
        self.db.commit()

    def observe(self, world, item, age_seconds: int, received_at=NOW, quality="GOOD", observed_at=None):
        ts = observed_at or NOW - timedelta(seconds=age_seconds)
        self.db.execute(
            """INSERT INTO telemetry_records(asset_id,metric_id,ts,value,source_id,received_at,quality,contract_version)
               VALUES (%s,%s,%s,21.5,%s,%s,%s,'v1')""",
            (item["asset"], self.metric_id, ts, world["source"], received_at, quality),
        )
        self.db.commit()
        return ts

    def batch(self, source_id, received_at=NOW, accepted=1, rejected=0, connector_status=None):
        return self.db.execute(
            """INSERT INTO ingestion_batches(source_id,batch_id,contract_version,payload_sha256,received_at,sent_at,
                       connector_name,connector_version,received_count,accepted_count,duplicate_count,rejected_count,
                       response_payload,connector_status)
               VALUES (%s,%s,'v1',%s,%s,%s,'e4b-test','1',%s,%s,0,%s,'{}'::jsonb,%s::jsonb)
               RETURNING ingestion_batch_id""",
            (source_id, uuid4(), hashlib.sha256(uuid4().bytes).hexdigest(), received_at,
             received_at - timedelta(seconds=1), accepted + rejected, accepted, rejected,
             json.dumps(connector_status or {"vendor_link": "CONNECTED"})),
        ).fetchone()[0]

    def run_job(self, now=NOW, config=None):
        self.db.commit()
        conn = da.connect(host="127.0.0.1", port=25443, dbname=DATABASE,
                          user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""))
        try:
            return run_once(conn, now, source_config=config)
        finally:
            conn.close()

    def current_source(self, source_id):
        return self.db.execute("""SELECT state,reason_code,last_received_at,expected_interval_seconds,
                                        active_series_count,stale_series_count,missing_series_count,mass_stale,
                                        last_success_at,last_error_at,last_error,connector_status
                                   FROM source_liveness_current WHERE source_id=%s""", (source_id,)).fetchone()

    def test_liveness_transitions_unknown_alive_silent_disabled_and_all_rejected_is_evidence(self):
        world = self.make_world()
        item = world[0]["assets"][0]
        self.observe(world[0], item, 1000)
        self.run_job()
        self.assertEqual(self.current_source(world[0]["source"])[0:2], ("UNKNOWN", "NEVER_RECEIVED"))

        self.db.execute("""UPDATE data_sources SET last_success_at=%s,last_error_at=%s,last_error='SCHEMA_INVALID: invalid'
                           WHERE source_id=%s""", (NOW - timedelta(days=1), NOW - timedelta(hours=1), world[0]["source"]))
        self.batch(world[0]["source"], accepted=0, rejected=1, connector_status={"vendor_link": "CONNECTED"})
        self.db.execute("UPDATE data_sources SET last_received_at=%s WHERE source_id=%s", (NOW, world[0]["source"]))
        self.run_job(now=NOW + timedelta(seconds=1))
        alive = self.current_source(world[0]["source"])
        self.assertEqual(alive[0:2], ("ALIVE", "KEEPALIVE_WITHIN_THRESHOLD"))
        self.assertEqual(alive[2:4], (NOW, 100))
        self.assertEqual(alive[8:11], (NOW - timedelta(days=1), NOW - timedelta(hours=1), "SCHEMA_INVALID: invalid"))
        self.assertEqual(alive[11], {"vendor_link": "CONNECTED"})

        self.run_job(now=NOW + timedelta(seconds=300))
        self.assertEqual(self.current_source(world[0]["source"])[0], "ALIVE")
        self.run_job(now=NOW + timedelta(seconds=201), config=SourceLivenessConfig(2, 3, 50))
        self.assertEqual(self.current_source(world[0]["source"])[0], "SILENT")
        self.db.execute("UPDATE data_sources SET status='DISABLED' WHERE source_id=%s", (world[0]["source"],))
        self.run_job(now=NOW + timedelta(seconds=302))
        self.assertEqual(self.current_source(world[0]["source"])[0], "DISABLED")
        transitions = self.db.execute("""SELECT from_state,to_state FROM source_liveness_transitions
                                          WHERE source_id=%s ORDER BY transition_id""", (world[0]["source"],)).fetchall()
        self.assertEqual([row[1] for row in transitions], ["UNKNOWN", "ALIVE", "SILENT", "DISABLED"])

    def test_mass_stale_thresholds_and_sources_are_independent_in_same_building(self):
        first, second = self.make_world((10, 10))[0:2]
        for idx, item in enumerate(first["assets"]):
            self.observe(first, item, 300 if idx < 2 else (600 if idx == 2 else 10))
        for idx, item in enumerate(second["assets"]):
            self.observe(second, item, 600 if idx < 6 else 10)
        for world in (first, second):
            self.db.execute("UPDATE data_sources SET last_received_at=%s WHERE source_id=%s", (NOW, world["source"]))
        self.run_job()
        a = self.current_source(first["source"])
        b = self.current_source(second["source"])
        self.assertEqual((a[4], a[5], a[6], a[7]), (10, 2, 1, False))
        self.assertEqual((b[4], b[5], b[6], b[7]), (10, 0, 6, True))
        causes = self.db.execute("""SELECT source_id,cause,count(*) FROM data_gaps WHERE gap_end IS NULL
                                   AND source_id=ANY(%s) GROUP BY source_id,cause ORDER BY source_id,cause""",
                                 ([first["source"], second["source"]],)).fetchall()
        self.assertEqual(causes, [(first["source"], "NO_DATA", 1), (second["source"], "SOURCE_SUSPECTED", 6)])

    def test_fewer_than_three_active_series_is_not_mass_stale_and_retired_assets_excluded(self):
        world = self.make_world((2,))[0]
        for item in world["assets"]:
            self.observe(world, item, 600)
        self.db.execute("UPDATE data_sources SET last_received_at=%s WHERE source_id=%s", (NOW, world["source"]))
        self.run_job()
        snapshot = self.current_source(world["source"])
        self.assertEqual((snapshot[4], snapshot[6], snapshot[7]), (2, 2, False))
        self.assertEqual(self.db.execute("SELECT count(*) FROM data_gaps WHERE source_id=%s AND cause='NO_DATA'",
                                         (world["source"],)).fetchone()[0], 2)

        third = self.make_world((1,))[0]
        self.observe(third, third["assets"][0], 600)
        self.db.execute("UPDATE assets SET lifecycle_status='RETIRED' WHERE asset_id=%s", (third["assets"][0]["asset"],))
        self.db.execute("UPDATE data_sources SET last_received_at=%s WHERE source_id=%s", (NOW, third["source"]))
        self.run_job()
        self.assertEqual(self.current_source(third["source"])[4:8], (0, 0, 0, False))
        self.assertEqual(self.db.execute("SELECT count(*) FROM data_gaps WHERE source_id=%s", (third["source"],)).fetchone()[0], 0)

    def test_open_gap_reclassification_is_audited_recovery_reclassifies_and_closed_gap_freezes(self):
        world = self.make_world()[0]
        item = world["assets"][0]
        start = self.observe(world, item, 1000)
        self.db.execute("UPDATE data_sources SET last_received_at=%s WHERE source_id=%s", (NOW, world["source"]))
        self.run_job()
        gap = self.db.execute("SELECT gap_id,cause,cause_updated_at FROM data_gaps WHERE source_id=%s AND gap_end IS NULL",
                              (world["source"],)).fetchone()
        self.assertEqual(gap[1], "NO_DATA")
        self.db.execute("UPDATE data_sources SET last_received_at=%s WHERE source_id=%s",
                        (NOW - timedelta(seconds=1000), world["source"]))
        self.run_job()
        suspected = self.db.execute("SELECT cause,cause_updated_at FROM data_gaps WHERE gap_id=%s", (gap[0],)).fetchone()
        self.assertEqual(suspected[0], "SOURCE_SUSPECTED")
        self.assertIsNotNone(suspected[1])
        self.assertNotEqual(suspected[1], gap[2])
        self.assertEqual(self.db.execute("SELECT count(*) FROM data_gap_cause_history WHERE gap_id=%s", (gap[0],)).fetchone()[0], 2)

        self.db.execute("UPDATE data_sources SET last_received_at=%s WHERE source_id=%s", (NOW + timedelta(seconds=1), world["source"]))
        self.run_job(now=NOW + timedelta(seconds=2))
        self.assertEqual(self.db.execute("SELECT cause FROM data_gaps WHERE gap_id=%s", (gap[0],)).fetchone()[0], "NO_DATA")
        self.observe(world, item, 0, received_at=NOW + timedelta(seconds=3), observed_at=NOW + timedelta(seconds=3))
        self.run_job(now=NOW + timedelta(seconds=4))
        frozen = self.db.execute("SELECT gap_start,gap_end,cause FROM data_gaps WHERE gap_id=%s", (gap[0],)).fetchone()
        self.assertGreater(frozen[1], frozen[0])
        self.db.execute("UPDATE data_sources SET last_received_at=%s WHERE source_id=%s",
                        (NOW - timedelta(days=1), world["source"]))
        self.run_job(now=NOW + timedelta(seconds=5))
        self.assertEqual(self.db.execute("SELECT gap_start,gap_end,cause FROM data_gaps WHERE gap_id=%s", (gap[0],)).fetchone(), frozen)
        self.assertEqual(self.db.execute("SELECT count(*) FROM alerts WHERE asset_id=%s", (item["asset"],)).fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM equipment_state_records WHERE asset_id=%s", (item["asset"],)).fetchone()[0], 0)
        self.assertIsNone(self.db.execute("SELECT state_id FROM v_asset_current_state WHERE asset_id=%s",
                                          (item["asset"],)).fetchone())

    def test_unknown_and_disabled_sources_leave_unknown_causes(self):
        world = self.make_world()[0]
        self.observe(world, world["assets"][0], 1000)
        self.run_job()
        gap_id, cause = self.db.execute("SELECT gap_id,cause FROM data_gaps WHERE source_id=%s", (world["source"],)).fetchone()
        self.assertEqual(cause, "UNKNOWN")
        self.db.execute("UPDATE data_sources SET status='DISABLED' WHERE source_id=%s", (world["source"],))
        self.run_job(now=NOW + timedelta(seconds=1))
        self.assertEqual(self.db.execute("SELECT cause FROM data_gaps WHERE gap_id=%s", (gap_id,)).fetchone()[0], "UNKNOWN")

    def test_job_rerun_is_idempotent_and_never_creates_offline_or_alerts(self):
        world = self.make_world()[0]
        self.observe(world, world["assets"][0], 1000)
        self.db.execute("UPDATE data_sources SET last_received_at=%s WHERE source_id=%s", (NOW, world["source"]))
        self.run_job()
        snapshot = self.db.execute("""SELECT state,evaluated_at,reason_code,active_series_count,missing_series_count,mass_stale
                                       FROM source_liveness_current WHERE source_id=%s""", (world["source"],)).fetchone()
        transitions = self.db.execute("SELECT count(*) FROM source_liveness_transitions WHERE source_id=%s", (world["source"],)).fetchone()[0]
        gap_id = self.db.execute("SELECT gap_id FROM data_gaps WHERE source_id=%s AND gap_end IS NULL",
                                 (world["source"],)).fetchone()[0]
        causes = self.db.execute("SELECT count(*) FROM data_gap_cause_history WHERE gap_id=%s", (gap_id,)).fetchone()[0]
        self.run_job()
        self.assertEqual(self.db.execute("""SELECT state,evaluated_at,reason_code,active_series_count,missing_series_count,mass_stale
                                            FROM source_liveness_current WHERE source_id=%s""", (world["source"],)).fetchone(), snapshot)
        self.assertEqual(self.db.execute("SELECT count(*) FROM source_liveness_transitions WHERE source_id=%s", (world["source"],)).fetchone()[0], transitions)
        self.assertEqual(self.db.execute("SELECT count(*) FROM data_gap_cause_history WHERE gap_id=%s",
                                         (gap_id,)).fetchone()[0], causes)
        self.assertNotIn("OFFLINE", {row[0] for row in self.db.execute("SELECT state FROM source_liveness_current WHERE source_id=%s", (world["source"],)).fetchall()})
        self.assertEqual(self.db.execute("SELECT count(*) FROM alerts WHERE asset_id=%s", (world["assets"][0]["asset"],)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
