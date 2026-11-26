"""maintenance.candidates against PostgreSQL 16 + psycopg 3, only in a disposable Compose runner."""
from __future__ import annotations

import os
import copy
from pathlib import Path
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from app.domain.evidence.envelope_evidence import validate_envelope_evidence
from app.domain.tools import maintenance_candidates as mc

ROOT = Path(__file__).resolve().parents[2]
HOST = os.environ.get("PGHOST", "")
PORT = os.environ.get("PGPORT", "5432")
USER = os.environ.get("PGUSER", "postgres")
PASSWORD = os.environ.get("PGPASSWORD", "")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
PRE_013 = ["003_incidents", "004_chat_views", "005_alert_uniqueness", "006_ingestion_foundation",
           "007_event_source_identity", "008_ingestion_contract_conformance", "009_freshness_expectations",
           "010_data_gap_lifecycle", "011_source_liveness", "012_asset_state_shadow"]
NOW = datetime(2026, 11, 26, tzinfo=timezone.utc)


class MaintenanceCandidatesPostgresTests(unittest.TestCase):
    def setUp(self):
        if not HOST or HOST in LOCAL_HOSTS:
            self.fail("Run only inside an isolated Compose runner with PGHOST=db; host database connections are forbidden.")
        self.database = f"anzma_maint_{uuid4().hex[:10]}"
        self.admin = psycopg.connect(host=HOST, port=PORT, dbname="postgres", user=USER, password=PASSWORD,
                                     autocommit=True)
        self.admin.execute(f'CREATE DATABASE "{self.database}"')
        self.conn = psycopg.connect(host=HOST, port=PORT, dbname=self.database, user=USER, password=PASSWORD,
                                    autocommit=True, row_factory=dict_row)
        self.addCleanup(self.cleanup_database)
        for name in ["base", *PRE_013]:
            path = ROOT / ("db/init/anzma_schema_and_seed.sql" if name == "base" else f"db/migrations/{name}.sql")
            self.conn.execute(path.read_text(encoding="utf-8"))
        env = dict(os.environ, PGDATABASE=self.database)
        run = subprocess.run([sys.executable, str(ROOT / "scripts/apply_migrations.py"), "--yes"],
                             cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.seed()

    def cleanup_database(self):
        self.conn.close()
        self.admin.execute(f'DROP DATABASE IF EXISTS "{self.database}" WITH (FORCE)')
        self.admin.close()

    def seed(self):
        self.conn.execute("UPDATE buildings SET timezone='Africa/Cairo' WHERE building_code='MAIN-TOWER'")
        self.building_id = self.conn.execute("SELECT building_id FROM buildings WHERE building_code='MAIN-TOWER'").fetchone()["building_id"]
        self.location_id = self.conn.execute("SELECT location_id FROM locations WHERE building_id=%s ORDER BY location_id LIMIT 1",
                                             (self.building_id,)).fetchone()["location_id"]
        self.system_id = self.conn.execute("SELECT system_type_id FROM system_types WHERE code='HVAC'").fetchone()["system_type_id"]
        self.metric_id = self.conn.execute("SELECT metric_id FROM metric_types WHERE name='temperature' AND unit='C'").fetchone()["metric_id"]
        self.event_type_id = self.conn.execute("SELECT event_type_id FROM event_types WHERE name='hvac_fault'").fetchone()["event_type_id"]
        self.source_id = self.conn.execute("""INSERT INTO data_sources(source_code,building_id,connector_name,connector_version)
                                              VALUES ('MAINT-SRC',%s,'test','1') RETURNING source_id""",
                                           (self.building_id,)).fetchone()["source_id"]
        self.asset_id = self.conn.execute("""INSERT INTO assets(asset_code,name,asset_type,system_type_id,location_id)
                                             VALUES ('MAINT-AHU','Test AHU','device',%s,%s) RETURNING asset_id""",
                                          (self.system_id, self.location_id)).fetchone()["asset_id"]
        self.conn.execute("INSERT INTO asset_external_ids(source_id,external_id,asset_id) VALUES (%s,'EXT-AHU',%s)",
                          (self.source_id, self.asset_id))
        self.event_ids = []
        for suffix, ts in (("1", "2026-11-25T12:00:00Z"), ("2", "2026-11-25T13:00:00Z")):
            self.event_ids.append(self.conn.execute("""INSERT INTO event_records(asset_id,event_type_id,ts,severity,source_id,source_event_id)
                                                       VALUES (%s,%s,%s,'warning',%s,%s) RETURNING event_id""",
                                                  (self.asset_id, self.event_type_id, ts, self.source_id,
                                                   f"MAINT-FAULT-{suffix}")).fetchone()["event_id"])
        self.telemetry_id = self.conn.execute("""INSERT INTO telemetry_records(asset_id,metric_id,ts,value,quality,source_id)
                                                VALUES (%s,%s,'2026-11-25T14:00:00Z',30.5,'GOOD',%s)
                                                RETURNING telemetry_id""",
                                             (self.asset_id, self.metric_id, self.source_id)).fetchone()["telemetry_id"]

    @staticmethod
    def fully_covered(_building, start, end, codes):
        return {code: mc.MaintenanceSourceCoverage(True, True, True, start, end, end) for code in codes}

    def run_tool(self, provider=None, **overrides):
        self.conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        arguments = {"building": "MAIN-TOWER", "start": "2026-11-25", "end": "2026-11-25", "systems": ["HVAC"]}
        arguments.update(overrides)
        return mc.run_maintenance_candidates(self.conn.cursor(), **arguments, coverage_provider=provider,
                                             confirmed_timezone_buildings=frozenset({"MAIN-TOWER"}),
                                             clock=lambda: NOW).to_dict()

    def test_postgres_query_scopes_scores_and_resolves_real_evidence(self):
        result = self.run_tool(self.fully_covered)
        self.assertEqual(result["tool"], "maintenance.candidates")
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertEqual((row["asset_code"], row["fault_count"], row["condition_metric"], row["condition_value"]),
                         ("MAINT-AHU", 2, "temperature", 30.5))
        self.assertEqual(row["condition_score"], 75.0)
        self.assertEqual(row["score"], 60.0)  # 60% * fault score 50 + 40% * condition score 75
        self.assertEqual(row["priority"], "MEDIUM")
        self.assertEqual({ref["kind"] for ref in row["evidence_refs"]}, {"event_record", "telemetry_record"})
        validation = validate_envelope_evidence(self.conn.cursor(), result, require_read_only=True)
        self.assertTrue(validation.ok, validation.errors)
        self.assertEqual(validation.verified_refs, 3)
        changed = copy.deepcopy(result)
        telemetry_ref = next(ref for ref in changed["rows"][0]["evidence_refs"] if ref["kind"] == "telemetry_record")
        telemetry_ref["value"] += 1
        bad_value = validate_envelope_evidence(self.conn.cursor(), changed, require_read_only=True)
        self.assertFalse(bad_value.ok)
        self.assertIn("EVIDENCE_VALUE_MISMATCH", {issue.code for issue in bad_value.blocking_issues})

    def test_telemetry_reference_from_another_building_is_rejected(self):
        annex_id = self.conn.execute("SELECT building_id FROM buildings WHERE building_code='DATA-CENTER-ANNEX'").fetchone()["building_id"]
        annex_location = self.conn.execute("SELECT location_id FROM locations WHERE building_id=%s ORDER BY location_id LIMIT 1",
                                           (annex_id,)).fetchone()["location_id"]
        asset_id = self.conn.execute("""INSERT INTO assets(asset_code,name,asset_type,system_type_id,location_id)
                                         VALUES ('MAINT-ANNEX-AHU','Annex AHU','device',%s,%s) RETURNING asset_id""",
                                    (self.system_id, annex_location)).fetchone()["asset_id"]
        telemetry_id, observed_at = self.conn.execute("""INSERT INTO telemetry_records(asset_id,metric_id,ts,value,quality)
                                                         VALUES (%s,%s,'2026-11-25T15:00:00Z',31.0,'GOOD')
                                                         RETURNING telemetry_id,ts""",
                                                      (asset_id, self.metric_id)).fetchone().values()
        result = {"parameters": {"building": "MAIN-TOWER"}, "rows": [{"building_code": "MAIN-TOWER",
                  "asset_code": "MAINT-AHU", "evidence_refs": [{"kind": "telemetry_record",
                  "id": str(telemetry_id), "observed_at": observed_at.isoformat(), "metric_code": "temperature",
                  "value": 31.0, "unit": "C", "quality": "GOOD"}]}]}
        self.conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        out = validate_envelope_evidence(self.conn.cursor(), result, require_read_only=True)
        self.assertFalse(out.ok)
        self.assertIn("EVIDENCE_BUILDING_MISMATCH", {issue.code for issue in out.blocking_issues})

    def test_no_trusted_coverage_withholds_score_and_keeps_observed_reasons(self):
        result = self.run_tool()
        self.assertEqual(result["status"], "PARTIAL")
        row = result["rows"][0]
        self.assertIsNone(row["score"])
        self.assertEqual(row["priority"], "INSUFFICIENT_DATA")
        self.assertEqual(row["data_sufficiency"], "INSUFFICIENT")
        self.assertEqual(row["fault_count"], 2)
        self.assertIn("score withheld because source history completeness is unconfirmed", row["reasons"])

    def test_bad_quality_reading_is_not_condition_evidence(self):
        self.conn.execute("UPDATE telemetry_records SET quality='BAD' WHERE telemetry_id=%s", (self.telemetry_id,))
        result = self.run_tool(self.fully_covered)
        row = next(r for r in result["rows"] if r["asset_code"] == "MAINT-AHU")
        self.assertIsNone(row["condition_score"])
        self.assertIsNone(row["score"])
        self.assertEqual(row["data_sufficiency"], "INSUFFICIENT")
        self.assertNotIn("telemetry_record", {ref["kind"] for ref in row["evidence_refs"]})

    def test_uncertain_condition_evidence_is_marked_limited(self):
        self.conn.execute("UPDATE telemetry_records SET quality='UNCERTAIN' WHERE telemetry_id=%s", (self.telemetry_id,))
        result = self.run_tool(self.fully_covered)
        row = next(r for r in result["rows"] if r["asset_code"] == "MAINT-AHU")
        self.assertEqual(row["condition_quality"], "UNCERTAIN")
        self.assertEqual(row["data_sufficiency"], "LIMITED")
        self.assertEqual(result["quality"], "UNCERTAIN")

    def test_missing_condition_measurement_does_not_become_a_healthy_zero(self):
        self.conn.execute("DELETE FROM telemetry_records WHERE telemetry_id=%s", (self.telemetry_id,))
        result = self.run_tool(self.fully_covered)
        row = next(r for r in result["rows"] if r["asset_code"] == "MAINT-AHU")
        self.assertIsNone(row["condition_score"])
        self.assertIsNone(row["score"])
        self.assertEqual(row["priority"], "INSUFFICIENT_DATA")
        self.assertIn("no supported canonical measurement", " ".join(row["reasons"]))

    def test_query_isolates_fault_and_condition_rows_to_requested_building(self):
        annex_id = self.conn.execute(
            "SELECT building_id FROM buildings WHERE building_code='DATA-CENTER-ANNEX'"
        ).fetchone()["building_id"]
        annex_location = self.conn.execute(
            "SELECT location_id FROM locations WHERE building_id=%s ORDER BY location_id LIMIT 1", (annex_id,)
        ).fetchone()["location_id"]
        annex_source = self.conn.execute(
            """INSERT INTO data_sources(source_code,building_id,connector_name,connector_version)
               VALUES ('MAINT-ANNEX-SRC',%s,'test','1') RETURNING source_id""", (annex_id,)
        ).fetchone()["source_id"]
        annex_asset = self.conn.execute(
            """INSERT INTO assets(asset_code,name,asset_type,system_type_id,location_id)
               VALUES ('MAINT-ANNEX-AHU-2','Annex AHU 2','device',%s,%s) RETURNING asset_id""",
            (self.system_id, annex_location),
        ).fetchone()["asset_id"]
        self.conn.execute(
            """INSERT INTO event_records(asset_id,event_type_id,ts,severity,source_id,source_event_id)
               VALUES (%s,%s,'2026-11-25T12:00:00Z','warning',%s,'ANNEX-FAULT-1')""",
            (annex_asset, self.event_type_id, annex_source),
        )
        self.conn.execute(
            """INSERT INTO telemetry_records(asset_id,metric_id,ts,value,quality,source_id)
               VALUES (%s,%s,'2026-11-25T14:00:00Z',35.0,'GOOD',%s)""",
            (annex_asset, self.metric_id, annex_source),
        )
        result = self.run_tool(self.fully_covered)
        self.assertEqual({row["building_code"] for row in result["rows"]}, {"MAIN-TOWER"})
        self.assertNotIn("MAINT-ANNEX-AHU-2", {row["asset_code"] for row in result["rows"]})

    def test_availability_candidate_reuses_real_offline_frequency_sessions(self):
        network_id = self.conn.execute("SELECT system_type_id FROM system_types WHERE code='NETWORKING'").fetchone()["system_type_id"]
        bandwidth_id = self.conn.execute("SELECT metric_id FROM metric_types WHERE name='bandwidth_util' AND unit='%' ").fetchone()["metric_id"]
        down_id = self.conn.execute("SELECT event_type_id FROM event_types WHERE name='link_down'").fetchone()["event_type_id"]
        up_id = self.conn.execute("SELECT event_type_id FROM event_types WHERE name='link_up'").fetchone()["event_type_id"]
        source_id = self.conn.execute(
            """INSERT INTO data_sources(source_code,building_id,connector_name,connector_version)
               VALUES ('MAINT-NET-SRC',%s,'test','1') RETURNING source_id""", (self.building_id,)
        ).fetchone()["source_id"]
        asset_id = self.conn.execute(
            """INSERT INTO assets(asset_code,name,asset_type,system_type_id,location_id)
               VALUES ('MAINT-SW','Test switch','device',%s,%s) RETURNING asset_id""",
            (network_id, self.location_id),
        ).fetchone()["asset_id"]
        self.conn.execute("INSERT INTO asset_external_ids(source_id,external_id,asset_id) VALUES (%s,'EXT-SW',%s)",
                          (source_id, asset_id))
        for event_type_id, observed_at, external_event_id in (
            (up_id, "2026-11-24T12:00:00Z", "SW-UP-0"),
            (down_id, "2026-11-25T10:00:00Z", "SW-DOWN-1"),
            (up_id, "2026-11-25T11:00:00Z", "SW-UP-1"),
        ):
            self.conn.execute(
                """INSERT INTO event_records(asset_id,event_type_id,ts,severity,source_id,source_event_id)
                   VALUES (%s,%s,%s,'info',%s,%s)""",
                (asset_id, event_type_id, observed_at, source_id, external_event_id),
            )
        self.conn.execute(
            """INSERT INTO telemetry_records(asset_id,metric_id,ts,value,quality,source_id)
               VALUES (%s,%s,'2026-11-25T14:00:00Z',85.0,'GOOD',%s)""",
            (asset_id, bandwidth_id, source_id),
        )
        result = self.run_tool(self.fully_covered, systems=["NETWORKING"])
        row = next(r for r in result["rows"] if r["asset_code"] == "MAINT-SW")
        self.assertEqual(row["scoring_profile"], "availability-v1")
        self.assertEqual((row["downtime_seconds"], row["downtime_score"], row["condition_score"]), (3600, 25.0, 75.0))
        self.assertEqual((row["score"], row["priority"]), (27.5, "LOW"))
        self.assertIn("context from devices.offline_frequency", " ".join(row["reasons"]))


if __name__ == "__main__":
    unittest.main()
