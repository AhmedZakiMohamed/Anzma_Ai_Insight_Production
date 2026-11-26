"""doors.after_hours SQL against a real PostgreSQL schema (disposable database only).

Builds the schema like test_event_catalogue_migration.py (base seed + migrations, with 013/014 through the real runner),
seeds one building with ACCESS_CONTROL doors and checks what the static SQL returns. The session is switched to READ ONLY
before the tool runs, which proves the tool issues only SELECTs. Requires psycopg and a reachable PostgreSQL server.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from app.domain.tools import doors_after_hours as dah
from app.domain.tools.coverage import SourceCoverage

ROOT = Path(__file__).resolve().parents[2]
HOST = os.environ.get("PGHOST", "127.0.0.1")
PORT = os.environ.get("PGPORT", "5432")
USER = os.environ.get("PGUSER", "postgres")
PASSWORD = os.environ.get("PGPASSWORD", "")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
PRE_013 = ["003_incidents", "004_chat_views", "005_alert_uniqueness", "006_ingestion_foundation",
           "007_event_source_identity", "008_ingestion_contract_conformance", "009_freshness_expectations",
           "010_data_gap_lifecycle", "011_source_liveness", "012_asset_state_shadow"]


class DoorsAfterHoursSqlTests(unittest.TestCase):
    def setUp(self):
        self.database = f"anzma_doors_{uuid4().hex[:10]}"
        self.admin = psycopg.connect(host=HOST, port=PORT, dbname="postgres", user=USER, password=PASSWORD, autocommit=True)
        self.admin.execute(f'CREATE DATABASE "{self.database}"')
        self.conn = psycopg.connect(host=HOST, port=PORT, dbname=self.database, user=USER, password=PASSWORD,
                                    autocommit=True, row_factory=dict_row)
        self.addCleanup(self.cleanup_database)
        for name in ["base", *PRE_013]:
            path = ROOT / ("db/init/anzma_schema_and_seed.sql" if name == "base" else f"db/migrations/{name}.sql")
            self.conn.execute(path.read_text(encoding="utf-8"))
        env = dict(os.environ, PGDATABASE=self.database)
        args = [sys.executable, str(ROOT / "scripts/apply_migrations.py"), "--yes"]
        if HOST.strip().lower() in LOCAL_HOSTS:
            args += ["--allow-host-target", self.database]
        result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.seed()

    # apply_migrations.py gives the two seed buildings these codes before migration 013
    MAIN, ANNEX = "MAIN-TOWER", "DATA-CENTER-ANNEX"

    def cleanup_database(self):
        self.conn.close()
        self.admin.execute(f'DROP DATABASE IF EXISTS "{self.database}" WITH (FORCE)')
        self.admin.close()

    def seed(self):
        q = lambda sql, *a: self.conn.execute(sql, a).fetchone()
        self.conn.execute("UPDATE buildings SET timezone = 'Africa/Cairo' WHERE building_id = 1")  # explicit, not backfill
        self.source_id = q("""INSERT INTO data_sources(source_code,building_id,connector_name,connector_version)
                              VALUES ('SRC-IT-DOORS',1,'it','1.0') RETURNING source_id""")["source_id"]
        st = q("SELECT system_type_id FROM system_types WHERE code='ACCESS_CONTROL'")["system_type_id"]
        self.assets = {}
        for code in ("IT-DOOR-1", "IT-DOOR-2"):
            self.assets[code] = q("""INSERT INTO assets(asset_code,name,asset_type,system_type_id,location_id)
                                     VALUES (%s,%s,'door_reader',%s,1) RETURNING asset_id""", code, f"Door {code}", st)["asset_id"]
            self.conn.execute("INSERT INTO asset_external_ids(source_id,external_id,asset_id) VALUES (%s,%s,%s)",
                              (self.source_id, f"EXT-{code}", self.assets[code]))
        # the OTHER building has its own source and door, which must never leak into MAIN-TOWER results.
        # It keeps the migration-009 backfilled timezone (Etc/GMT-2) on purpose.
        src2 = q("""INSERT INTO data_sources(source_code,building_id,connector_name,connector_version)
                    VALUES ('SRC-IT-ANNEX',2,'it','1.0') RETURNING source_id""")["source_id"]
        other = q("""INSERT INTO assets(asset_code,name,asset_type,system_type_id,location_id)
                     VALUES ('IT-DOOR-OTHER','Other','door_reader',%s,4) RETURNING asset_id""", st)["asset_id"]
        self.conn.execute("INSERT INTO asset_external_ids(source_id,external_id,asset_id) VALUES (%s,'EXT-OTHER',%s)", (src2, other))
        self.types = {r["name"]: r["event_type_id"] for r in self.conn.execute("SELECT * FROM event_types").fetchall()}
        events = [  # window for 2026-11-01 Cairo = [18:00Z, 22:00Z)
            ("IT-DOOR-1", "door_opened", "2026-11-01T17:59:59Z", "S1"), ("IT-DOOR-1", "door_opened", "2026-11-01T18:00:00Z", "S2"),
            ("IT-DOOR-1", "door_opened", "2026-11-01T21:59:59Z", "S3"), ("IT-DOOR-1", "door_opened", "2026-11-01T22:00:00Z", "S4"),
            ("IT-DOOR-1", "door_forced", "2026-11-01T20:00:01Z", "S5"), ("IT-DOOR-2", "door_opened", "2026-11-01T20:00:00Z", None),
            ("IT-DOOR-2", "access_granted", "2026-11-01T20:00:05Z", "S6"),
            ("IT-DOOR-1", "door_opened", "2026-11-02T20:00:00Z", "S7"),   # 2026-11-02 has no door_forced event
        ]
        for code, etype, ts, ident in events:
            self.conn.execute("""INSERT INTO event_records(asset_id,event_type_id,ts,severity,source_id,source_event_id)
                                 VALUES (%s,%s,%s,'info',%s,%s)""",
                              (self.assets[code], self.types[etype], ts, self.source_id if ident else None, ident))
        self.conn.execute("""INSERT INTO event_records(asset_id,event_type_id,ts,severity,source_id,source_event_id)
                             VALUES (%s,%s,'2026-11-01T20:30:00Z','info',%s,'O1')""", (other, self.types["door_opened"], src2))
        self.assertTrue({"door_opened", "door_forced", "access_granted"} <= set(self.types))  # migration 014 vocabulary

    def run_tool(self, provider=None, **kw):
        self.conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        args = dict(building="MAIN-TOWER", start="2026-11-01", end="2026-11-01")
        args.update(kw)
        return dah.run_doors_after_hours(self.conn.cursor(), coverage_provider=provider, **args).to_dict()

    def test_counts_boundaries_scoping_and_limitations(self):
        d = self.run_tool()
        rows = {r["asset_code"]: r for r in d["rows"]}
        self.assertEqual(set(rows), {"IT-DOOR-1", "IT-DOOR-2"})              # other building's door excluded
        self.assertEqual(rows["IT-DOOR-1"]["opening_count"], 2)             # 18:00:00Z in, 21:59:59Z in, 17:59:59Z and 22:00:00Z out
        self.assertEqual(rows["IT-DOOR-1"]["uncounted_door_forced_events"], 1)
        self.assertEqual(rows["IT-DOOR-1"]["door_external_id"], "EXT-IT-DOOR-1")
        self.assertEqual(rows["IT-DOOR-1"]["source_code"], "SRC-IT-DOORS")
        self.assertEqual(rows["IT-DOOR-2"]["opening_count"], 1)             # access_granted is not an opening
        codes = {x["code"] for x in d["limitations"]}
        self.assertTrue({"COMPLETION_NOT_CONFIRMED", "CORRELATION_UNAVAILABLE", "MISSING_EVENT_IDENTITY"} <= codes)
        self.assertEqual((d["status"], d["complete"]), ("PARTIAL", False))
        self.assertEqual(d["rows"][0]["asset_code"], "IT-DOOR-1")           # higher count first

    COVERED = staticmethod(lambda b, s, e, codes: {c: SourceCoverage(c, True, True, s, e, e) for c in codes})

    def test_complete_only_with_trusted_coverage_and_no_forced_events(self):
        d = self.run_tool(self.COVERED, start="2026-11-02", end="2026-11-02")     # no door_forced that day
        self.assertEqual((d["status"], d["complete"], d["limitations"]), ("COMPLETE", True, []))
        self.assertEqual((d["data_as_of"], d["rows"][0]["opening_count"]), ("2026-11-02T22:00:00Z", 1))
        self.assertEqual(d["source_ids"], ["SRC-IT-DOORS"])

    def test_door_forced_in_window_downgrades_even_with_trusted_coverage(self):
        d = self.run_tool(self.COVERED)                                            # 2026-11-01 has a door_forced event
        self.assertEqual((d["status"], d["complete"]), ("PARTIAL", False))
        self.assertEqual([x["code"] for x in d["limitations"]], ["CORRELATION_UNAVAILABLE", "MISSING_EVENT_IDENTITY"])

    def test_backfilled_timezone_building_is_not_complete(self):
        d = self.run_tool(self.COVERED, building="DATA-CENTER-ANNEX")
        rows = {r["asset_code"]: r["opening_count"] for r in d["rows"]}
        self.assertEqual(rows, {"IT-DOOR-OTHER": 1})                               # its own door only
        self.assertEqual((d["status"], d["parameters"]["timezone"]), ("PARTIAL", "Etc/GMT-2"))
        self.assertEqual([x["code"] for x in d["limitations"]], ["TIMEZONE_PROVENANCE_UNCONFIRMED"])
        confirmed = dah.run_doors_after_hours(self.conn.cursor(), building="DATA-CENTER-ANNEX", start="2026-11-01", end="2026-11-01",
                                              coverage_provider=self.COVERED,
                                              confirmed_timezone_buildings=frozenset({"DATA-CENTER-ANNEX"})).to_dict()
        self.assertEqual(confirmed["status"], "COMPLETE")

    def test_empty_window_list_runs_and_returns_no_rows(self):
        # a timestamp period entirely outside after-hours produces zero windows; empty arrays must bind and cast cleanly
        d = self.run_tool(self.COVERED, start="2026-11-01T08:00:00+02:00", end="2026-11-01T10:00:00+02:00")
        self.assertEqual((d["total_rows"], d["parameters"]["window_count"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
