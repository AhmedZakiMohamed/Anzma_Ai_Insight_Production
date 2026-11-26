"""Migration 014 (event catalogue vocabulary + open/close pairing) on disposable databases only.

Every test creates its own database from the base seed + migrations 003-012, then lets the real runner
(scripts/apply_migrations.py) apply 013 and 014, exactly like test_building_code_migration.py.
The expected catalogue is tests/fixtures/event_catalog_014.json, which tests/unit/test_event_catalog_parity.py
also compares with the legacy pair lists, so DB == JSON == legacy lists.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from uuid import uuid4

import psycopg
from psycopg import errors

ROOT = Path(__file__).resolve().parents[2]
HOST = os.environ.get("PGHOST", "127.0.0.1")
PORT = os.environ.get("PGPORT", "5432")
USER = os.environ.get("PGUSER", "postgres")
PASSWORD = os.environ.get("PGPASSWORD", "")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
PRE_013 = ["003_incidents", "004_chat_views", "005_alert_uniqueness", "006_ingestion_foundation",
           "007_event_source_identity", "008_ingestion_contract_conformance", "009_freshness_expectations",
           "010_data_gap_lifecycle", "011_source_liveness", "012_asset_state_shadow"]
MIGRATION_014 = ROOT / "db/migrations/014_event_catalogue_pairing.sql"
EXPECTED = json.loads((ROOT / "tests/fixtures/event_catalog_014.json").read_text(encoding="utf-8"))
CATALOGUE = EXPECTED["catalogue"]

CATALOGUE_QUERY = """
    SELECT c.name, c.event_class, c.outage_class, o.name AS resolves
      FROM event_types c LEFT JOIN event_types o ON o.event_type_id = c.resolves_event_type_id
     ORDER BY c.name"""


class EventCatalogueMigrationTests(unittest.TestCase):
    def setUp(self):
        self.database = f"anzma_event_catalogue_{uuid4().hex[:10]}"
        self.admin = psycopg.connect(host=HOST, port=PORT, dbname="postgres", user=USER,
                                     password=PASSWORD, autocommit=True)
        self.admin.execute(f'CREATE DATABASE "{self.database}"')
        self.conn = psycopg.connect(host=HOST, port=PORT, dbname=self.database, user=USER,
                                    password=PASSWORD, autocommit=True)
        for name in ["base", *PRE_013]:
            path = (ROOT / "db/init/anzma_schema_and_seed.sql" if name == "base" else
                    ROOT / "db/migrations" / f"{name}.sql")
            self.conn.execute(path.read_text(encoding="utf-8"))
        self.addCleanup(self.cleanup_database)

    def cleanup_database(self):
        self.conn.close()
        self.admin.execute(f'DROP DATABASE IF EXISTS "{self.database}" WITH (FORCE)')
        self.admin.close()

    def run_migrations(self):
        env = os.environ.copy()
        env["PGDATABASE"] = self.database
        args = [sys.executable, str(ROOT / "scripts/apply_migrations.py"), "--yes"]
        if HOST.strip().lower() in LOCAL_HOSTS:
            args += ["--allow-host-target", self.database]
        return subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True)

    def migrate(self):
        result = self.run_migrations()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def catalogue(self):
        return {name: {"event_class": cls, "outage_class": out, "resolves": res}
                for name, cls, out, res in self.conn.execute(CATALOGUE_QUERY).fetchall()}

    def compat(self):
        return set(self.conn.execute(
            """SELECT et.name, st.code FROM event_type_system_compat c
               JOIN event_types et USING (event_type_id) JOIN system_types st USING (system_type_id)""").fetchall())

    # ------------------------------------------------------------------ expected end state

    def test_seed_database_gets_the_expected_catalogue(self):
        self.assertEqual(self.conn.execute("SELECT count(*) FROM event_types").fetchone()[0], 12)
        compat_before = self.compat()
        result = self.migrate()
        self.assertIn("APPLIED 014_event_catalogue_pairing", result.stdout)
        self.assertEqual(self.catalogue(), CATALOGUE)
        expected_new_compat = {(name, system) for name, systems in EXPECTED["new_event_types"].items()
                               for system in systems}
        self.assertEqual(self.compat(), compat_before | expected_new_compat,
                         "existing compatibility rows must be untouched; only the two new types are added")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM schema_migrations WHERE version='014_event_catalogue_pairing'").fetchone()[0], 1)

    def test_catalogue_pair_invariants_hold_in_the_database(self):
        self.migrate()
        violations = self.conn.execute(
            """SELECT c.name FROM event_types c JOIN event_types o ON o.event_type_id = c.resolves_event_type_id
                WHERE o.event_class IS DISTINCT FROM 'FAULT_OPEN' OR o.outage_class IS DISTINCT FROM c.outage_class"""
        ).fetchall()
        self.assertEqual(violations, [])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM event_types WHERE event_class IS NULL").fetchone()[0], 0,
            "every seed catalogue type must be classified")

    def test_db_pairs_match_the_view_and_the_legacy_lists(self):
        """DB pairs vs the third hand-maintained location (v_camera_status) and the legacy pair lists."""
        self.migrate()
        db_pairs = set(self.conn.execute(
            """SELECT o.name, c.name FROM event_types c JOIN event_types o ON o.event_type_id = c.resolves_event_type_id"""
        ).fetchall())
        sys.path.insert(0, str(ROOT))
        from app.api import chat_facts
        from app.repositories import dashboard_core_pg as core
        self.assertEqual(db_pairs, set(core.FAULT_RECOVERY_PAIRS))
        self.assertEqual(db_pairs, set(chat_facts.PAIRS))
        view = self.conn.execute("SELECT pg_get_viewdef('v_camera_status'::regclass)").fetchone()[0]
        self.assertIn("camera_offline", view)
        self.assertIn("camera_online", view)

    def test_availability_pairs_are_the_only_offline_eligible_pairs(self):
        self.migrate()
        pairs = set(self.conn.execute(
            """SELECT o.name, c.name FROM event_types c JOIN event_types o ON o.event_type_id = c.resolves_event_type_id
                WHERE c.outage_class = 'AVAILABILITY'""").fetchall())
        self.assertEqual(pairs, {("link_down", "link_up"), ("camera_offline", "camera_online")})

    # ------------------------------------------------------------------ safety properties

    def test_reapplying_the_file_is_idempotent(self):
        self.migrate()
        before = (self.catalogue(), self.compat(),
                  self.conn.execute("SELECT event_type_id, name FROM event_types ORDER BY event_type_id").fetchall())
        for _ in range(2):
            self.conn.execute(MIGRATION_014.read_text(encoding="utf-8"))
        after = (self.catalogue(), self.compat(),
                 self.conn.execute("SELECT event_type_id, name FROM event_types ORDER BY event_type_id").fetchall())
        self.assertEqual(before, after)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM schema_migrations WHERE version='014_event_catalogue_pairing'").fetchone()[0], 1)

    def test_runner_does_not_reapply_014(self):
        self.migrate()
        second = self.migrate()
        self.assertNotIn("APPLIED 014", second.stdout)

    def test_existing_event_records_and_ids_are_untouched(self):
        ids_before = self.conn.execute("SELECT event_type_id, name FROM event_types ORDER BY event_type_id").fetchall()
        events_before = self.conn.execute("SELECT count(*), min(event_id), max(event_id) FROM event_records").fetchone()
        self.migrate()
        ids_after = self.conn.execute(
            "SELECT event_type_id, name FROM event_types WHERE event_type_id <= 12 ORDER BY event_type_id").fetchall()
        self.assertEqual(ids_before, ids_after, "the 12 seed ids must not change")
        self.assertEqual(events_before,
                         self.conn.execute("SELECT count(*), min(event_id), max(event_id) FROM event_records").fetchone())

    def test_operator_added_type_is_left_unclassified(self):
        self.conn.execute("INSERT INTO event_types(name) VALUES ('operator_custom_probe')")
        self.migrate()
        row = self.catalogue()["operator_custom_probe"]
        self.assertEqual(row, {"event_class": None, "outage_class": None, "resolves": None})

    def test_operator_provided_classification_is_not_overwritten(self):
        self.migrate()
        self.conn.execute("UPDATE event_types SET event_class='STATE_REPORT' WHERE name='door_forced'")
        self.conn.execute(MIGRATION_014.read_text(encoding="utf-8"))
        self.assertEqual(self.catalogue()["door_forced"]["event_class"], "STATE_REPORT")

    # ------------------------------------------------------------------ constraints reject bad shapes

    def assert_rejected(self, sql, error):
        with self.assertRaises(error, msg=sql):
            self.conn.execute(sql)

    def test_constraints_reject_inconsistent_catalogue_rows(self):
        self.migrate()
        self.assert_rejected("UPDATE event_types SET event_class='NOT_A_CLASS' WHERE name='door_forced'",
                             errors.CheckViolation)
        self.assert_rejected("UPDATE event_types SET outage_class='SOMETHING' WHERE name='link_down'",
                             errors.CheckViolation)
        # outage_class only on open/close classes
        self.assert_rejected("UPDATE event_types SET outage_class='AVAILABILITY' WHERE name='door_forced'",
                             errors.CheckViolation)
        # a FAULT_CLOSE must name what it resolves
        self.assert_rejected("UPDATE event_types SET event_class='FAULT_CLOSE', outage_class='FAULT' "
                             "WHERE name='door_forced'", errors.CheckViolation)
        # only a FAULT_CLOSE may resolve something
        self.assert_rejected("UPDATE event_types SET resolves_event_type_id="
                             "(SELECT event_type_id FROM event_types WHERE name='link_down') WHERE name='door_forced'",
                             errors.CheckViolation)
        # not itself
        self.assert_rejected("UPDATE event_types SET resolves_event_type_id=event_type_id WHERE name='link_up'",
                             errors.CheckViolation)
        # one closer per opener
        self.assert_rejected("UPDATE event_types SET resolves_event_type_id="
                             "(SELECT event_type_id FROM event_types WHERE name='link_down') "
                             "WHERE name='camera_online'", errors.UniqueViolation)
        self.assertEqual(self.catalogue(), CATALOGUE, "rejected statements must leave the catalogue unchanged")


if __name__ == "__main__":
    unittest.main()
