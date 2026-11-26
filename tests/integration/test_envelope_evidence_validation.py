"""Building-scoped Tool Envelope evidence checks against disposable PostgreSQL via psycopg 3."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from app.domain.evidence import evidence as alert_evidence
from app.domain.evidence.envelope_evidence import EvidenceCode, validate_envelope_evidence

ROOT = Path(__file__).resolve().parents[2]
HOST = os.environ.get("PGHOST", "")
PORT = os.environ.get("PGPORT", "")
USER = os.environ.get("PGUSER", "postgres")
PASSWORD = os.environ.get("PGPASSWORD", "")
PRE_013 = ["003_incidents", "004_chat_views", "005_alert_uniqueness", "006_ingestion_foundation",
           "007_event_source_identity", "008_ingestion_contract_conformance", "009_freshness_expectations",
           "010_data_gap_lifecycle", "011_source_liveness", "012_asset_state_shadow"]


class EnvelopeEvidencePostgresTests(unittest.TestCase):
    def setUp(self):
        if not HOST or HOST in {"127.0.0.1", "localhost", "::1", "host.docker.internal"}:
            self.fail("Run inside the disposable Compose API container with PGHOST=db; host DB connections are forbidden.")
        self.database = f"anzma_evidence_{uuid4().hex[:10]}"
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
        self._seed_facts()

    def cleanup_database(self):
        self.conn.close()
        self.admin.execute(f'DROP DATABASE IF EXISTS "{self.database}" WITH (FORCE)')
        self.admin.close()

    def _seed_facts(self):
        buildings = {r["building_code"]: r["building_id"] for r in self.conn.execute(
            "SELECT building_id, building_code FROM buildings").fetchall()}
        locs = {r["building_id"]: r["location_id"] for r in self.conn.execute(
            "SELECT location_id, building_id FROM locations ORDER BY location_id").fetchall()}
        systems = {r["code"]: r["system_type_id"] for r in self.conn.execute("SELECT code, system_type_id FROM system_types")}
        types = {r["name"]: r["event_type_id"] for r in self.conn.execute(
            "SELECT name, event_type_id FROM event_types")}
        self.assertIn("MAIN-TOWER", buildings)
        self.assertIn("DATA-CENTER-ANNEX", buildings)
        self.assets = {}
        for code, building_code, system in (("T7-DOOR-A", "MAIN-TOWER", "ACCESS_CONTROL"),
                                            ("T7-CAM-A", "MAIN-TOWER", "CCTV"),
                                            ("T7-CAM-B", "DATA-CENTER-ANNEX", "CCTV")):
            bid = buildings[building_code]
            aid = self.conn.execute("""INSERT INTO assets(asset_code,name,asset_type,system_type_id,location_id)
                                       VALUES (%s,%s,'device',%s,%s) RETURNING asset_id""",
                                    (code, code, systems[system], locs[bid])).fetchone()["asset_id"]
            self.assets[code] = aid
        self.event_ids = {}
        for code, event_type in (("T7-DOOR-A", "door_opened"), ("T7-CAM-A", "camera_offline"),
                                 ("T7-CAM-B", "camera_offline")):
            eid = self.conn.execute("""INSERT INTO event_records(asset_id,event_type_id,ts,severity)
                                       VALUES (%s,%s,'2026-11-25T12:00:00Z','info') RETURNING event_id""",
                                    (self.assets[code], types[event_type])).fetchone()["event_id"]
            self.event_ids[code] = eid

    def _result(self, building, *codes):
        refs = [{"kind": "event_record", "id": str(self.event_ids[c])} for c in codes]
        return {"parameters": {"building": building}, "rows": [{"building_code": building,
                                                                   "asset_code": "compound-evidence",
                                                                   "evidence_refs": refs}]}

    def _validate_read_only(self, result):
        self.conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        return validate_envelope_evidence(self.conn.cursor(), result)

    def test_two_assets_in_same_building_are_valid_compound_evidence(self):
        out = self._validate_read_only(self._result("MAIN-TOWER", "T7-DOOR-A", "T7-CAM-A"))
        self.assertTrue(out.ok, out.errors)
        self.assertEqual((out.unique_refs, out.verified_refs), (2, 2))

    def test_cross_building_reference_is_rejected(self):
        out = self._validate_read_only(self._result("MAIN-TOWER", "T7-DOOR-A", "T7-CAM-B"))
        self.assertFalse(out.ok)
        self.assertIn(EvidenceCode.EVIDENCE_BUILDING_MISMATCH, {i.code for i in out.blocking_issues})
        self.assertEqual(out.verified_refs, 1)

    def test_building_must_be_in_trusted_server_authorization(self):
        out = validate_envelope_evidence(self.conn.cursor(), self._result("MAIN-TOWER", "T7-DOOR-A"),
                                         authorized_building_codes={"DATA-CENTER-ANNEX"}, require_read_only=False)
        self.assertFalse(out.ok)
        self.assertEqual(out.blocking_issues[0].code, EvidenceCode.BUILDING_NOT_AUTHORIZED)

    def test_alert_evidence_allows_cross_asset_reference_inside_same_building(self):
        rows = [(self.assets["T7-DOOR-A"], {"source": "event", "record_id": self.event_ids["T7-CAM-A"],
                                             "ts": "2026-11-25T12:00:00+00:00", "event_type": "CAMERA_OFFLINE"})]
        self.assertEqual(alert_evidence._verify(self.conn.cursor(), rows), [None])

    def test_alert_evidence_rejects_reference_from_another_building(self):
        rows = [(self.assets["T7-DOOR-A"], {"source": "event", "record_id": self.event_ids["T7-CAM-B"],
                                             "ts": "2026-11-25T12:00:00+00:00", "event_type": "CAMERA_OFFLINE"})]
        problems = alert_evidence._verify(self.conn.cursor(), rows)
        self.assertEqual(problems, ["evidence belongs to a different building than the alert"])


if __name__ == "__main__":
    unittest.main()
