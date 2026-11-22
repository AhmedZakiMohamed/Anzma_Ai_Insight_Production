"""CLI integration tests; guarded to run only against a named anzma_e2_* database."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from uuid import uuid4
from pathlib import Path

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


ROOT = Path(__file__).resolve().parents[2]
DATABASE = os.environ.get("ANZMA_E2_TEST_DATABASE", "")


@unittest.skipUnless(psycopg and DATABASE.startswith("anzma_e2_"),
                     "set ANZMA_E2_TEST_DATABASE to a dedicated anzma_e2_* database")
class FreshnessAdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = psycopg.connect(
            host=os.environ.get("PGHOST", "127.0.0.1"),
            port=os.environ.get("PGPORT", "5432"),
            dbname=DATABASE,
            user=os.environ.get("PGUSER", "postgres"),
            password=os.environ.get("PGPASSWORD", ""),
        )
        suffix = uuid4().hex[:12]
        cls.building_name = f"E2 admin test {suffix}"
        cls.building_code = f"E2-ADMIN-BLD-{suffix}"
        cls.asset_code = f"E2-ADMIN-{suffix}"
        cls.building_id = cls.db.execute(
            "INSERT INTO buildings (name,building_code) VALUES (%s,%s) RETURNING building_id",
            (cls.building_name, cls.building_code),
        ).fetchone()[0]
        cls.location_id = cls.db.execute(
            "INSERT INTO locations (building_id,floor,name) VALUES (%s,0,'E2 admin test') RETURNING location_id",
            (cls.building_id,),
        ).fetchone()[0]
        system_type_id = cls.db.execute(
            "SELECT system_type_id FROM system_types WHERE code='HVAC'"
        ).fetchone()[0]
        cls.asset_id = cls.db.execute(
            """INSERT INTO assets (asset_code,name,asset_type,system_type_id,location_id)
               VALUES (%s,'E2 admin test asset','test',%s,%s) RETURNING asset_id""",
            (cls.asset_code, system_type_id, cls.location_id),
        ).fetchone()[0]
        cls.metric_code = cls.db.execute(
            "SELECT name FROM metric_types ORDER BY metric_id LIMIT 1"
        ).fetchone()[0]
        cls.db.commit()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "db"):
            cls.db.execute("DELETE FROM reporting_expectations WHERE asset_id=%s", (cls.asset_id,))
            cls.db.execute("DELETE FROM assets WHERE asset_id=%s", (cls.asset_id,))
            cls.db.execute("DELETE FROM locations WHERE location_id=%s", (cls.location_id,))
            cls.db.execute("DELETE FROM buildings WHERE building_id=%s", (cls.building_id,))
            cls.db.commit()
            cls.db.close()

    def run_cli(self, *args):
        env = os.environ.copy()
        env["PGDATABASE"] = DATABASE
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts/freshness_admin.py"), *args],
            cwd=ROOT, env=env, text=True, capture_output=True, check=False,
        )

    def test_schedule_update_commits_and_emits_json_audit(self):
        result = self.run_cli(
            "set-building-schedule", "--building-id", str(self.building_id),
            "--timezone", "America/New_York", "--work-start", "07:30",
            "--work-end", "16:30", "--non-working-weekdays", "6,7",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        audit = json.loads(next(line for line in result.stderr.splitlines() if line.startswith("{")))
        self.assertEqual(audit["event"], "BUILDING_SCHEDULE_SET")
        self.assertEqual(audit["outcome"], "SUCCESS")
        row = self.db.execute(
            "SELECT timezone,work_start_local,work_end_local,non_working_weekdays FROM buildings WHERE building_id=%s",
            (self.building_id,),
        ).fetchone()
        self.assertEqual((row[0], str(row[1]), str(row[2]), row[3]),
                         ("America/New_York", "07:30:00", "16:30:00", [6, 7]))
        self.db.commit()

    def test_invalid_timezone_does_not_change_schedule(self):
        before = self.db.execute(
            "SELECT timezone,work_start_local,work_end_local,non_working_weekdays FROM buildings WHERE building_id=%s",
            (self.building_id,),
        ).fetchone()
        self.db.commit()
        result = self.run_cli(
            "set-building-schedule", "--building-id", str(self.building_id),
            "--timezone", "Not/A_Real_Zone", "--work-start", "08:00", "--work-end", "18:00",
        )
        self.assertEqual(result.returncode, 2)
        after = self.db.execute(
            "SELECT timezone,work_start_local,work_end_local,non_working_weekdays FROM buildings WHERE building_id=%s",
            (self.building_id,),
        ).fetchone()
        self.assertEqual(after, before)
        self.db.commit()

    def test_expectation_upsert_increments_version_and_audits_commit(self):
        args = (
            "set-expectation", "--scope", "ASSET_METRIC", "--record-kind", "telemetry",
            "--asset-code", self.asset_code, "--metric-code", self.metric_code,
            "--expected-interval-seconds", "120", "--priority", "71",
        )
        first = self.run_cli(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        first_audit = json.loads(next(line for line in first.stderr.splitlines() if line.startswith("{")))
        self.assertEqual(first_audit["outcome"], "SUCCESS")
        second_args = (*args[:-4], "--expected-interval-seconds", "240", "--priority", "71")
        second = self.run_cli(*second_args)
        self.assertEqual(second.returncode, 0, second.stderr)
        second_audit = json.loads(next(line for line in second.stderr.splitlines() if line.startswith("{")))
        self.assertEqual(second_audit["event"], "REPORTING_EXPECTATION_SET")
        row = self.db.execute(
            """SELECT expected_interval_seconds,expectation_version FROM reporting_expectations
               WHERE scope='ASSET_METRIC' AND asset_id=(SELECT asset_id FROM assets WHERE asset_code=%s)
                 AND metric_code=%s AND priority=71""",
            (self.asset_code, self.metric_code),
        ).fetchone()
        self.assertEqual(row, (240, 2))
        self.db.commit()

    def test_unknown_building_is_failure_without_partial_change(self):
        before = self.db.execute(
            "SELECT count(*) FROM buildings WHERE building_id=%s", (self.building_id,)
        ).fetchone()[0]
        result = self.run_cli(
            "set-building-schedule", "--building-id", "99999999", "--timezone", "Etc/GMT-2",
            "--work-start", "08:00", "--work-end", "18:00",
        )
        self.assertEqual(result.returncode, 1)
        audit = json.loads(next(line for line in result.stderr.splitlines() if line.startswith("{")))
        self.assertEqual(audit["event"], "BUILDING_SCHEDULE_SET")
        self.assertEqual(audit["outcome"], "FAILED")
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM buildings WHERE building_id=%s", (self.building_id,)).fetchone()[0],
            before,
        )
        self.db.commit()

    def test_list_returns_json(self):
        result = self.run_cli("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertIn("buildings", document)
        self.assertIn("expectations", document)


if __name__ == "__main__":
    unittest.main(verbosity=2)
