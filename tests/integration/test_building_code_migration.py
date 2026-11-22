"""Migration 013 on fresh seed and pre-existing databases, using disposable DBs only."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest
from uuid import uuid4

import psycopg


ROOT = Path(__file__).resolve().parents[2]
HOST = os.environ.get("PGHOST", "127.0.0.1")
PORT = os.environ.get("PGPORT", "5432")
USER = os.environ.get("PGUSER", "postgres")
PASSWORD = os.environ.get("PGPASSWORD", "")
MIGRATIONS = ["003_incidents", "004_chat_views", "005_alert_uniqueness",
              "006_ingestion_foundation", "007_event_source_identity",
              "008_ingestion_contract_conformance", "009_freshness_expectations",
              "010_data_gap_lifecycle", "011_source_liveness", "012_asset_state_shadow"]


class BuildingCodeMigrationTests(unittest.TestCase):
    def setUp(self):
        self.database = f"anzma_building_code_{uuid4().hex[:10]}"
        self.admin = psycopg.connect(host=HOST, port=PORT, dbname="postgres", user=USER,
                                     password=PASSWORD, autocommit=True)
        self.admin.execute(f'CREATE DATABASE "{self.database}"')
        self.conn = psycopg.connect(host=HOST, port=PORT, dbname=self.database, user=USER,
                                    password=PASSWORD, autocommit=True)
        for name in ["base", *MIGRATIONS]:
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
        return subprocess.run([sys.executable, str(ROOT / "scripts/apply_migrations.py")],
                              cwd=ROOT, env=env, capture_output=True, text=True)

    def test_fresh_seed_buildings_receive_deterministic_codes(self):
        result = self.run_migrations()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("REGISTERED 2 seeded building code(s)", result.stdout)
        rows = self.conn.execute(
            "SELECT name,building_code FROM buildings ORDER BY building_id"
        ).fetchall()
        self.assertEqual(rows, [("Main Tower", "MAIN-TOWER"),
                                ("Data Center Annex", "DATA-CENTER-ANNEX")])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM schema_migrations WHERE version='013_building_code_required'"
        ).fetchone()[0], 1)

    def test_other_uncoded_building_keeps_actionable_migration_failure(self):
        self.conn.execute("INSERT INTO buildings(name) VALUES ('Operator site')")
        failed = self.run_migrations()
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("Register stable codes with scripts/ingestion_admin.py register-building", failed.stderr)
        self.conn.execute("UPDATE buildings SET building_code='OPERATOR-SITE' WHERE name='Operator site'")
        retried = self.run_migrations()
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        codes = dict(self.conn.execute("SELECT name,building_code FROM buildings").fetchall())
        self.assertEqual(codes["Main Tower"], "MAIN-TOWER")
        self.assertEqual(codes["Data Center Annex"], "DATA-CENTER-ANNEX")
        self.assertEqual(codes["Operator site"], "OPERATOR-SITE")


if __name__ == "__main__":
    unittest.main()
