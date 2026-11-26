"""devices.offline_frequency SQL against a real PostgreSQL schema (disposable database only).

Builds the schema like test_doors_after_hours_sql.py / test_event_catalogue_migration.py (base seed + migrations, with
013/014 through the real runner: the tool depends on migration 014's event_class / outage_class / resolves_event_type_id),
seeds one building with CCTV and NETWORKING devices plus a second building, and checks what the static SQL returns. The
session is switched to READ ONLY before the tool runs, which proves it issues only SELECTs.
Requires psycopg and a reachable PostgreSQL server.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from app.domain.tools import devices_offline_frequency as dof
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
NOW = datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc)


class DevicesOfflineFrequencySqlTests(unittest.TestCase):
    # apply_migrations.py gives the two seed buildings these codes before migration 013
    MAIN, ANNEX = "MAIN-TOWER", "DATA-CENTER-ANNEX"
    COVERED = staticmethod(lambda b, s, e, codes: {c: SourceCoverage(c, True, True, s, e, e) for c in codes})

    def setUp(self):
        self.database = f"anzma_offline_{uuid4().hex[:10]}"
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

    def cleanup_database(self):
        self.conn.close()
        self.admin.execute(f'DROP DATABASE IF EXISTS "{self.database}" WITH (FORCE)')
        self.admin.close()

    def seed(self):
        q = lambda sql, *a: self.conn.execute(sql, a).fetchone()
        self.conn.execute("UPDATE buildings SET timezone = 'Africa/Cairo' WHERE building_id = 1")   # explicit, not the 009 backfill
        sys_id = {r["code"]: r["system_type_id"] for r in self.conn.execute("SELECT * FROM system_types").fetchall()}
        self.types = {r["name"]: r["event_type_id"] for r in self.conn.execute("SELECT * FROM event_types").fetchall()}
        self.sources = {}
        for code, bld in (("SRC-IT-CCTV", 1), ("SRC-IT-NET", 1), ("SRC-IT-HVAC", 1), ("SRC-IT-ANNEX", 2)):
            self.sources[code] = q("""INSERT INTO data_sources(source_code,building_id,connector_name,connector_version)
                                      VALUES (%s,%s,'it','1.0') RETURNING source_id""", code, bld)["source_id"]
        self.assets = {}

        def asset(code, system, source, location=1, external=True):
            self.assets[code] = q("""INSERT INTO assets(asset_code,name,asset_type,system_type_id,location_id)
                                     VALUES (%s,%s,'device',%s,%s) RETURNING asset_id""", code, f"Device {code}", sys_id[system], location)["asset_id"]
            if external:
                self.conn.execute("INSERT INTO asset_external_ids(source_id,external_id,asset_id) VALUES (%s,%s,%s)",
                                  (self.sources[source], f"EXT-{code}", self.assets[code]))

        for code in ("IT-CAM-1", "IT-CAM-2", "IT-CAM-3", "IT-CAM-4", "IT-CAM-5", "IT-CAM-6", "IT-CAM-7"):
            asset(code, "CCTV", "SRC-IT-CCTV", external=code != "IT-CAM-4")      # CAM-4 is a legacy asset: events with no source
        asset("IT-SW-1", "NETWORKING", "SRC-IT-NET")
        asset("IT-AHU-1", "HVAC", "SRC-IT-HVAC")
        asset("IT-CAM-OTHER", "CCTV", "SRC-IT-ANNEX", location=4)                  # other building: must never leak

        def event(code, etype, ts, ident, source="SRC-IT-CCTV"):
            self.conn.execute("""INSERT INTO event_records(asset_id,event_type_id,ts,severity,source_id,source_event_id)
                                 VALUES (%s,%s,%s,'info',%s,%s)""",
                              (self.assets[code], self.types[etype], ts, self.sources[source] if ident else None, ident))

        off, on = "camera_offline", "camera_online"
        # period 2026-11-02 Cairo = [2026-11-01T22:00Z, 2026-11-02T22:00Z)
        # CAM-1: recovered, then down since 11-01 20:00 (repeated alarm 21:00), recovered 11-02 01:00 (ongoing at start,
        #        3 h overlap), a merged outage 05:00-05:30 (two alarms), and an open one from 20:00 (2 h to period end)
        for ts, t, i in (("2026-10-31T10:00:00Z", off, "A1"), ("2026-10-31T11:00:00Z", on, "A2"), ("2026-11-01T20:00:00Z", off, "A3"),
                         ("2026-11-01T21:00:00Z", off, "A4"), ("2026-11-02T01:00:00Z", on, "A5"), ("2026-11-02T05:00:00Z", off, "A6"),
                         ("2026-11-02T05:05:00Z", off, "A7"), ("2026-11-02T05:30:00Z", on, "A8"), ("2026-11-02T20:00:00Z", off, "A9")):
            event("IT-CAM-1", t, ts, i)
        # CAM-2: recovered before the period, then a recovery with no start inside the period (orphan)
        for ts, t, i in (("2026-10-31T10:00:00Z", off, "B1"), ("2026-10-31T11:00:00Z", on, "B2"), ("2026-11-02T12:00:00Z", on, "B3")):
            event("IT-CAM-2", t, ts, i)
        # CAM-3: silent. No events at all: it must produce no row and no outage.
        # CAM-4: legacy rows (source_id and source_event_id NULL), never merged
        for ts, t in (("2026-11-02T10:00:00Z", off), ("2026-11-02T10:00:01Z", off), ("2026-11-02T10:30:00Z", on)):
            event("IT-CAM-4", t, ts, None)
        # CAM-5: an offline exactly at the exclusive period end, so outside the period
        event("IT-CAM-5", off, "2026-11-02T22:00:00Z", "C1")
        # CAM-6: an offline exactly at the inclusive period start
        event("IT-CAM-6", off, "2026-11-01T22:00:00Z", "D1")
        event("IT-CAM-6", on, "2026-11-01T22:10:00Z", "D2")
        # CAM-7: down since long before the period, no event inside it
        event("IT-CAM-7", off, "2026-10-15T08:00:00Z", "E1")
        # SW-1: link_down/link_up pairs (NETWORKING)
        for ts, t, i in (("2026-11-02T09:00:00Z", "link_down", "F1"), ("2026-11-02T09:02:00Z", "link_up", "F2"),
                         ("2026-11-02T09:03:00Z", "link_down", "F3"), ("2026-11-02T09:04:30Z", "link_up", "F4")):
            event("IT-SW-1", t, ts, i, "SRC-IT-NET")
        # AHU-1: fault pair, never an outage
        event("IT-AHU-1", "hvac_fault", "2026-11-02T07:00:00Z", "G1", "SRC-IT-HVAC")
        event("IT-AHU-1", "hvac_recovered", "2026-11-02T07:45:00Z", "G2", "SRC-IT-HVAC")
        # other building
        event("IT-CAM-OTHER", off, "2026-11-02T11:00:00Z", "H1", "SRC-IT-ANNEX")

    def run_tool(self, provider=None, **kw):
        self.conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        args = dict(building=self.MAIN, start="2026-11-02", end="2026-11-02")
        args.update(kw)
        return dof.run_devices_offline_frequency(self.conn.cursor(), coverage_provider=provider, clock=lambda: NOW, **args).to_dict()

    def rows(self, d):
        return {r["asset_code"]: r for r in d["rows"]}

    def test_sessions_metrics_and_scoping(self):
        d = self.run_tool(self.COVERED)
        rows = self.rows(d)
        # silent CAM-3, CAM-5 (event at the exclusive end), the HVAC unit and the other building's camera have no row;
        # CAM-2 has one only because of its in-period orphan recovery
        self.assertEqual(set(rows), {"IT-CAM-1", "IT-CAM-2", "IT-CAM-4", "IT-CAM-6", "IT-CAM-7", "IT-SW-1"})
        self.assertEqual(d["total_rows"], 6)

        c1 = rows["IT-CAM-1"]
        self.assertEqual((c1["initial_state"], c1["outages_started_in_period"], c1["outages_ongoing_at_period_start"],
                          c1["downtime_seconds_in_period"], c1["open_at_period_end"], c1["duration_is_lower_bound"]),
                         ("OFFLINE", 2, 1, 10800 + 1800 + 7200, True, True))
        s = c1["sessions"]
        self.assertEqual([(x["start_utc"], x["end_utc"], x["overlap_seconds"]) for x in s],
                         [("2026-11-01T20:00:00Z", "2026-11-02T01:00:00Z", 10800), ("2026-11-02T05:00:00Z", "2026-11-02T05:30:00Z", 1800),
                          ("2026-11-02T20:00:00Z", None, 7200)])
        self.assertEqual([x["repeated_start_events"] for x in s], [1, 1, 0])             # repeated alarms merged, not new outages
        self.assertEqual((s[2]["cutoff_utc"], s[2]["cutoff_reason"]), ("2026-11-02T22:00:00Z", "period_end"))
        self.assertEqual([x["event_type"] for x in s[0]["evidence_refs"]], ["camera_offline", "camera_offline", "camera_online"])
        self.assertEqual([x["id"] for x in c1["initial_state_evidence_refs"]], [x["id"] for x in c1["evidence_refs"][:3]])
        self.assertEqual(len(c1["initial_state_evidence_refs"]), 3)                      # anchor recovery, first start, last earlier alarm
        self.assertEqual((c1["source_code"], c1["asset_external_id"], c1["system_type"]), ("SRC-IT-CCTV", "EXT-IT-CAM-1", "CCTV"))

        c2 = rows["IT-CAM-2"]
        self.assertEqual((c2["initial_state"], c2["sessions"], c2["orphan_offline_end_count"], c2["downtime_seconds_in_period"]), ("ONLINE", [], 1, 0))

        c4 = rows["IT-CAM-4"]                                                              # legacy rows: NULL source, NULL identity
        self.assertEqual((c4["source_code"], c4["asset_external_id"], c4["outages_started_in_period"], c4["downtime_seconds_in_period"]),
                         (None, None, 1, 1800))
        self.assertEqual(len(c4["sessions"][0]["evidence_refs"]), 3)

        c6 = rows["IT-CAM-6"]                                                              # event exactly at start is inside
        self.assertEqual((c6["outages_started_in_period"], c6["outages_ongoing_at_period_start"], c6["downtime_seconds_in_period"]), (1, 0, 600))

        c7 = rows["IT-CAM-7"]                                                              # down for the whole period, no in-period event
        self.assertEqual((c7["initial_state"], c7["outages_started_in_period"], c7["outages_ongoing_at_period_start"],
                          c7["downtime_seconds_in_period"], c7["open_at_period_end"]), ("OFFLINE", 0, 1, 86400, True))
        self.assertEqual(c7["sessions"][0]["start_utc"], "2026-10-15T08:00:00Z")

        sw = rows["IT-SW-1"]
        self.assertEqual((sw["system_type"], sw["outages_started_in_period"], sw["downtime_seconds_in_period"]), ("NETWORKING", 2, 210))

    def test_ordering_and_limitations(self):
        d = self.run_tool(self.COVERED)
        self.assertEqual([r["asset_code"] for r in d["rows"]],      # started desc, then building, source_code ('' for NULL first), external id
                         ["IT-CAM-1", "IT-SW-1", "IT-CAM-4", "IT-CAM-6", "IT-CAM-2", "IT-CAM-7"])
        lim = {x["code"]: x for x in d["limitations"]}
        self.assertEqual(set(lim), {"OPEN_OUTAGE_LOWER_BOUND", "INITIAL_STATE_UNKNOWN", "ORPHAN_OFFLINE_END", "MISSING_EVENT_IDENTITY",
                                    "FLAPPING_NOT_EVALUATED"})
        self.assertEqual(lim["OPEN_OUTAGE_LOWER_BOUND"]["details"], {"devices": 2, "open_sessions": 2})
        self.assertEqual(lim["MISSING_EVENT_IDENTITY"]["details"], {"events_without_identity": 3})
        self.assertEqual((d["status"], d["complete"], d["data_as_of"]), ("COMPLETE", True, "2026-11-02T22:00:00Z"))

    def test_source_ids_only_cover_systems_in_scope(self):
        d = self.run_tool(self.COVERED)
        self.assertEqual(d["source_ids"], ["SRC-IT-CCTV", "SRC-IT-NET"])               # the HVAC source is not an offline source

    def test_without_provider_is_partial_and_period_boundaries_are_half_open(self):
        d = self.run_tool()
        self.assertEqual((d["status"], d["complete"], d["data_as_of"]), ("PARTIAL", False, None))
        self.assertNotIn("IT-CAM-5", self.rows(d))                                         # offline exactly at the exclusive end

    def test_system_filter_and_unsupported_fault_systems(self):
        net = self.run_tool(self.COVERED, systems=["NETWORKING"])
        self.assertEqual(list(self.rows(net)), ["IT-SW-1"])
        self.assertEqual(net["source_ids"], ["SRC-IT-NET"])
        hv = self.run_tool(self.COVERED, systems=["HVAC", "FIRE_ALARM"])
        self.assertEqual((hv["status"], hv["rows"], hv["limitations"][0]["code"]), ("UNSUPPORTED", [], "SYSTEM_NOT_ELIGIBLE_FOR_OFFLINE"))
        self.assertEqual(self.run_tool(self.COVERED, systems=["TOASTERS"])["status"], "CANNOT_COMPUTE")

    def test_source_filter_including_legacy_rows(self):
        d = self.run_tool(self.COVERED, source="SRC-IT-NET")
        self.assertEqual(list(self.rows(d)), ["IT-SW-1"])                                  # legacy (NULL source) rows are excluded by a source filter

    def test_period_after_the_outages_has_unknown_or_recovered_devices_and_one_ongoing(self):
        d = self.run_tool(self.COVERED, start="2026-11-03", end="2026-11-03")
        rows = self.rows(d)
        self.assertEqual(set(rows), {"IT-CAM-1", "IT-CAM-5", "IT-CAM-7"})
        # CAM-5 went offline exactly at this period's inclusive start: started here, and (see the 11-02 test) not there
        self.assertEqual((rows["IT-CAM-5"]["outages_started_in_period"], rows["IT-CAM-5"]["outages_ongoing_at_period_start"],
                          rows["IT-CAM-5"]["open_at_period_end"]), (1, 0, True))
        self.assertEqual((rows["IT-CAM-1"]["initial_state"], rows["IT-CAM-1"]["outages_ongoing_at_period_start"],
                          rows["IT-CAM-1"]["outages_started_in_period"]), ("OFFLINE", 1, 0))

    def test_rank_by_downtime(self):
        d = self.run_tool(self.COVERED, rank_by="downtime_seconds")
        self.assertEqual([r["asset_code"] for r in d["rows"]][:2], ["IT-CAM-7", "IT-CAM-1"])    # 86400 s, then 19800 s

    def test_other_building_and_unknown_building(self):
        d = self.run_tool(self.COVERED, building=self.ANNEX)
        self.assertEqual(list(self.rows(d)), ["IT-CAM-OTHER"])
        self.assertEqual((d["status"], d["parameters"]["timezone"]), ("PARTIAL", "Etc/GMT-2"))   # 009 backfill value is not trusted
        self.assertEqual(self.run_tool(building="NOPE")["status"], "CANNOT_COMPUTE")

    def test_empty_period_is_a_complete_zero_only_with_coverage(self):
        d = self.run_tool(self.COVERED, start="2025-01-01", end="2025-01-01")
        self.assertEqual((d["status"], d["rows"], d["total_rows"], d["limitations"]), ("COMPLETE", [], 0, []))

    def test_catalogue_without_migration_014_is_cannot_compute(self):
        self.conn.execute("ALTER TABLE event_types DROP COLUMN resolves_event_type_id CASCADE")
        d = self.run_tool(self.COVERED)
        self.assertEqual((d["status"], d["rows"], d["limitations"][0]["code"]), ("CANNOT_COMPUTE", [], "CATALOGUE_NOT_MIGRATED"))

    def test_session_is_read_only(self):
        self.conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
            self.conn.execute("DELETE FROM event_records")
        d = dof.run_devices_offline_frequency(self.conn.cursor(), building=self.MAIN, start="2026-11-02", end="2026-11-02",
                                              coverage_provider=self.COVERED, clock=lambda: NOW).to_dict()
        self.assertEqual(d["status"], "COMPLETE")


if __name__ == "__main__":
    unittest.main()
