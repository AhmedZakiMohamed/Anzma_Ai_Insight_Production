"""Identity mappings on PostgreSQL 16 in a disposable Compose network only."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from app.domain.identity import IdentityConflict, IdentityMappings, IdentityNotFound
from app.repositories.identity_pg import IdentityPostgresStore

ROOT = Path(__file__).resolve().parents[2]
HOST = os.environ.get("PGHOST", "")
PORT = os.environ.get("PGPORT", "5432")
USER = os.environ.get("PGUSER", "postgres")
PASSWORD = os.environ.get("PGPASSWORD", "")
PRE_013 = ["003_incidents", "004_chat_views", "005_alert_uniqueness", "006_ingestion_foundation",
           "007_event_source_identity", "008_ingestion_contract_conformance", "009_freshness_expectations",
           "010_data_gap_lifecycle", "011_source_liveness", "012_asset_state_shadow"]


class IdentityMappingsPostgresTests(unittest.TestCase):
    def setUp(self):
        if HOST != "db":
            self.fail("Run only inside the isolated Compose runner with PGHOST=db; host database targets are forbidden.")
        self.database = f"anzma_identity_{uuid4().hex[:10]}"
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
        result = subprocess.run([sys.executable, str(ROOT / "scripts/apply_migrations.py"), "--yes"],
                                cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        self.building_a = self.conn.execute(
            "SELECT building_id FROM buildings WHERE building_code='MAIN-TOWER'"
        ).fetchone()["building_id"]
        self.building_b = self.conn.execute(
            "SELECT building_id FROM buildings WHERE building_code='DATA-CENTER-ANNEX'"
        ).fetchone()["building_id"]
        self.main_location = self.conn.execute(
            "SELECT location_id FROM locations WHERE building_id=%s ORDER BY location_id LIMIT 1",
            (self.building_a,),
        ).fetchone()["location_id"]
        self.annex_location = self.conn.execute(
            "SELECT location_id FROM locations WHERE building_id=%s ORDER BY location_id LIMIT 1",
            (self.building_b,),
        ).fetchone()["location_id"]
        self.access_system_id = self.conn.execute(
            "SELECT system_type_id FROM system_types WHERE code='ACCESS_CONTROL'"
        ).fetchone()["system_type_id"]
        self.hvac_system_id = self.conn.execute(
            "SELECT system_type_id FROM system_types WHERE code='HVAC'"
        ).fetchone()["system_type_id"]
        self.source_code = f"IDENTITY-A-{uuid4().hex[:8]}"
        self.source_b_code = f"IDENTITY-B-{uuid4().hex[:8]}"
        self.source_id = self._create_source(self.source_code, self.building_a)
        self.source_b_id = self._create_source(self.source_b_code, self.building_a)
        self.door_a = self._create_asset("ACCESS_CONTROL", self.main_location)
        self.door_b = self._create_asset("ACCESS_CONTROL", self.main_location)
        self.hvac = self._create_asset("HVAC", self.main_location)
        self.other_building_asset = self._create_asset("ACCESS_CONTROL", self.annex_location)
        self.identities = IdentityMappings(IdentityPostgresStore(self.conn.cursor()))

    def cleanup_database(self):
        self.conn.close()
        self.admin.execute(f'DROP DATABASE IF EXISTS "{self.database}" WITH (FORCE)')
        self.admin.close()

    def _create_source(self, source_code, building_id):
        return self.conn.execute(
            """INSERT INTO data_sources(source_code,building_id,connector_name,connector_version)
               VALUES (%s,%s,'identity-test','1') RETURNING source_id""", (source_code, building_id),
        ).fetchone()["source_id"]

    def _create_asset(self, system_code, location_id):
        return self.conn.execute(
            """INSERT INTO assets(asset_code,name,asset_type,system_type_id,location_id)
               SELECT %s,%s,'device',system_type_id,%s FROM system_types WHERE code=%s
               RETURNING asset_code""",
            (f"IDENTITY-ASSET-{uuid4().hex[:10]}", "Identity test asset", location_id, system_code),
        ).fetchone()["asset_code"]

    def test_mapping_retry_is_idempotent_and_returns_existing_canonical_identity(self):
        first = self.identities.map_asset(source_code=self.source_code, external_id="vendor:door-01",
                                          asset_code=self.door_a)
        retry = self.identities.map_asset(source_code=self.source_code, external_id="vendor:door-01",
                                          asset_code=self.door_a)
        self.assertTrue(first.created)
        self.assertFalse(retry.created)
        self.assertEqual(retry.mapping.asset_code, self.door_a)
        self.assertEqual(retry.mapping.building_id, self.building_a)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM asset_external_ids WHERE source_id=%s AND external_id=%s",
            (self.source_id, "vendor:door-01"),
        ).fetchone()["n"], 1)

    def test_conflicting_reassignment_is_rejected_and_original_row_is_preserved(self):
        original = self.identities.map_asset(source_code=self.source_code, external_id="vendor:door-02",
                                            asset_code=self.door_a).mapping
        with self.assertRaisesRegex(IdentityConflict, "already mapped to a different asset"):
            self.identities.map_asset(source_code=self.source_code, external_id="vendor:door-02",
                                      asset_code=self.door_b)
        persisted = self.identities.resolve_asset(source_code=self.source_code, external_id="vendor:door-02")
        self.assertEqual(persisted, original)
        self.assertEqual(persisted.asset_code, self.door_a)

    def test_same_external_id_is_unique_per_source_not_globally(self):
        a = self.identities.map_asset(source_code=self.source_code, external_id="vendor:shared-serial",
                                      asset_code=self.door_a).mapping
        b = self.identities.map_asset(source_code=self.source_b_code, external_id="vendor:shared-serial",
                                      asset_code=self.door_b).mapping
        self.assertEqual(a.external_id, b.external_id)
        self.assertNotEqual(a.source_id, b.source_id)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM asset_external_ids WHERE external_id=%s", ("vendor:shared-serial",),
        ).fetchone()["n"], 2)

    def test_cross_building_and_non_access_control_door_mappings_are_rejected(self):
        with self.assertRaisesRegex(IdentityNotFound, "source's building"):
            self.identities.map_asset(source_code=self.source_code, external_id="vendor:wrong-site",
                                      asset_code=self.other_building_asset)
        with self.assertRaisesRegex(IdentityConflict, "requires a ACCESS_CONTROL asset"):
            self.identities.map_door(source_code=self.source_code, external_id="vendor:not-a-door",
                                     asset_code=self.hvac)
        self.assertIsNone(self.identities.resolve_asset(source_code=self.source_code, external_id="vendor:wrong-site"))


if __name__ == "__main__":
    unittest.main()
