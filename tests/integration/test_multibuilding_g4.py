"""End-to-end G4 proof. Run only inside the named disposable G4 compose project."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient


EXPECTED_PROJECT = "anzma-g4-proof-20261123"
if os.getenv("ANZMA_G4_DISPOSABLE_PROJECT") != EXPECTED_PROJECT or os.getenv("PGHOST") != "db":
    raise RuntimeError(f"Run only with ANZMA_G4_DISPOSABLE_PROJECT={EXPECTED_PROJECT} inside its compose API container")

from app.repositories import dashboard_core_pg as core  # noqa: E402
from scripts.freshness_job import run_pass  # noqa: E402
from app.main import api  # noqa: E402


def canonical_hash(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(payload).hexdigest()


class MultiBuildingG4Tests(unittest.TestCase):
    """One isolated integration proof; rows intentionally remain in its disposable DB."""

    @classmethod
    def setUpClass(cls):
        cls.conn = psycopg.connect(
            host=os.environ["PGHOST"], port=int(os.environ["PGPORT"]), dbname=os.environ["PGDATABASE"],
            user=os.environ["PGUSER"], password=os.getenv("PGPASSWORD", ""), row_factory=psycopg.rows.dict_row,
        )
        cls.client = TestClient(api)
        cls.headers = {"Authorization": f"Bearer {os.environ['ANZMA_API_TOKEN']}"}
        cls.suffix = uuid4().hex[:10].upper()
        cls.now = datetime.now(timezone.utc).replace(microsecond=0)
        with cls.conn.cursor() as cur:
            cur.execute("SELECT building_id FROM buildings WHERE building_code='MAIN-TOWER'")
            cls.building_a_id = cur.fetchone()["building_id"]
            cur.execute("SELECT timezone,work_start_local,work_end_local FROM buildings WHERE building_id=%s",
                        (cls.building_a_id,))
            cls.building_a_schedule = cur.fetchone()
            cur.execute("SELECT asset_id FROM assets WHERE asset_code='HVAC-AHU-01'")
            cls.asset_a_id = cur.fetchone()["asset_id"]
            cur.execute("SELECT max(ts) AS last FROM (SELECT max(ts) ts FROM v_telemetry_valid UNION ALL SELECT max(ts) ts FROM event_records) x")
            last = cur.fetchone()["last"]
            cls.window_end = last + timedelta(seconds=1)
            cls.window_start = last - timedelta(hours=24)
        cls.conn.commit()
        cls.client.get("/health")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _a_snapshot(self):
        # The public dashboard routes have no building filter; use the same repository
        # projection with a building id and fixed pre-ingestion time bounds for A.
        summary = core.get_dashboard_summary(
            self.conn, building_id=self.building_a_id,
            start_time=core.format_ts(self.window_start), end_time=core.format_ts(self.window_end),
        )
        alerts = self.client.get("/alerts?limit=500", headers=self.headers).json()["items"]
        incidents = self.client.get("/incidents", headers=self.headers).json()["incidents"]
        freshness = self.client.get("/api/v1/freshness?building_code=MAIN-TOWER&limit=500", headers=self.headers).json()
        sources = self.client.get("/api/v1/sources/liveness?building_code=MAIN-TOWER&limit=500", headers=self.headers).json()
        gaps = self.client.get("/api/v1/data-gaps?building_code=MAIN-TOWER&status=all&limit=500", headers=self.headers).json()
        asset_ids = {row["asset_id"] for row in self.conn.execute(
            "SELECT asset_id FROM assets a JOIN locations l USING(location_id) WHERE l.building_id=%s",
            (self.building_a_id,),
        ).fetchall()}
        stable = {
            # KPIs use the same explicit A building and fixed time bounds. Global
            # data-extent metadata is expected to move when B adds newer records.
            "overview_kpis": summary["kpis"],
            "alerts": [x for x in alerts if x["asset_id"] in asset_ids],
            "incidents": [x for x in incidents if x["building"] == "Main Tower"],
            "freshness": freshness["items"], "sources": sources["items"], "gaps": gaps["items"],
        }
        return stable

    def test_013_enforces_required_unique_building_code(self):
        with self.conn.cursor() as cur:
            cur.execute("SAVEPOINT g4_null_code")
            with self.assertRaises(psycopg.errors.NotNullViolation):
                cur.execute("INSERT INTO buildings(name,building_code) VALUES (%s,NULL)",
                            (f"invalid G4 null {self.suffix}",))
            cur.execute("ROLLBACK TO SAVEPOINT g4_null_code")
            cur.execute("RELEASE SAVEPOINT g4_null_code")
            cur.execute("SELECT building_code FROM buildings WHERE building_id=%s", (self.building_a_id,))
            existing_code = cur.fetchone()["building_code"]
            cur.execute("SAVEPOINT g4_duplicate_code")
            with self.assertRaises(psycopg.errors.UniqueViolation):
                cur.execute("INSERT INTO buildings(name,building_code) VALUES (%s,%s)",
                            (f"duplicate G4 code {self.suffix}", existing_code))
            cur.execute("ROLLBACK TO SAVEPOINT g4_duplicate_code")
            cur.execute("RELEASE SAVEPOINT g4_duplicate_code")
        self.conn.commit()

    def test_second_building_isolated_through_contract_and_evaluators(self):
        before = self._a_snapshot()
        self.assertNotEqual("America/New_York", self.building_a_schedule["timezone"])
        building_code = f"G4-{self.suffix}"
        building_name = f"G4 North Campus {self.suffix}"
        source_code = f"G4S-{self.suffix}"
        asset_code = f"G4A-{self.suffix}"
        external_id = f"vendor:{self.suffix}:ahu-01"
        prefix = secrets.token_hex(6)
        key = f"azk_{prefix}_{secrets.token_urlsafe(32)}"
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO buildings(name,building_code,timezone,work_start_local,work_end_local,non_working_weekdays)
                   VALUES (%s,%s,'America/New_York','07:00','16:00',ARRAY[6,7]::smallint[]) RETURNING building_id""",
                (building_name, building_code),
            )
            building_id = cur.fetchone()["building_id"]
            cur.execute("INSERT INTO locations(building_id,floor,name) VALUES (%s,1,%s) RETURNING location_id",
                        (building_id, f"Mechanical {self.suffix}"))
            location_id = cur.fetchone()["location_id"]
            cur.execute("SELECT system_type_id FROM system_types WHERE code='HVAC'")
            system_id = cur.fetchone()["system_type_id"]
            cur.execute("INSERT INTO assets(asset_code,name,asset_type,system_type_id,location_id) VALUES (%s,%s,'ahu',%s,%s) RETURNING asset_id",
                        (asset_code, f"North Campus AHU {self.suffix}", system_id, location_id))
            asset_id = cur.fetchone()["asset_id"]
            cur.execute("INSERT INTO data_sources(source_code,building_id,connector_name,connector_version) VALUES (%s,%s,'g4-proof','1.0') RETURNING source_id",
                        (source_code, building_id))
            source_id = cur.fetchone()["source_id"]
            cur.execute("INSERT INTO source_credentials(source_id,key_prefix,key_hash) VALUES (%s,%s,%s)",
                        (source_id, prefix, hashlib.sha256(key.encode()).hexdigest()))
            cur.execute("INSERT INTO asset_external_ids(source_id,external_id,asset_id) VALUES (%s,%s,%s)",
                        (source_id, external_id, asset_id))
        self.conn.commit()

        def stamp(value):
            return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

        def envelope(records):
            return {"contract_version": "1.0", "batch_id": str(uuid4()), "sent_at": stamp(self.now),
                    "source": {"source_code": source_code, "connector": {"name": "g4-proof", "version": "1.0"},
                               "connector_status": {"vendor_link": "UP", "buffered_records": 0}},
                    "building_code": building_code, "records": records}

        headers = {"Authorization": f"Bearer {key}"}
        records = [
            {"record_type": "telemetry", "asset": {"external_id": external_id}, "metric_code": "temperature",
             "value": 22.4, "unit": "C", "observed_at": stamp(self.now)},
            {"record_type": "telemetry", "asset": {"external_id": external_id}, "metric_code": "humidity",
             "value": 44.0, "unit": "%", "observed_at": stamp(self.now - timedelta(hours=3))},
            {"record_type": "event", "source_event_id": f"event-{self.suffix}", "asset": {"external_id": external_id},
             "event_type": "hvac_recovered", "severity": "INFO", "observed_at": stamp(self.now - timedelta(minutes=1))},
            {"record_type": "state", "asset": {"external_id": external_id}, "observed_at": stamp(self.now),
             "reported_state": "RUNNING", "availability": "AVAILABLE"},
        ]
        accepted = self.client.post("/api/v1/ingest/batches", json=envelope(records), headers=headers)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["counts"]["accepted"], 4)
        keepalive = self.client.post("/api/v1/ingest/batches", json=envelope([]), headers=headers)
        self.assertEqual(keepalive.status_code, 200, keepalive.text)
        self.assertEqual(keepalive.json()["counts"]["received"], 0)

        foreign = {"record_type": "telemetry", "asset": {"asset_code": "HVAC-AHU-01"},
                   "metric_code": "temperature", "value": 21.0, "unit": "C", "observed_at": stamp(self.now)}
        denied = self.client.post("/api/v1/ingest/batches", json=envelope([foreign]), headers=headers)
        self.assertEqual(denied.status_code, 422)
        self.assertEqual(denied.json()["rejected"][0]["reason_code"], "ASSET_NOT_PERMITTED")

        # Compare A immediately after B's ingest and before the global job refreshes
        # its evaluation timestamps for every building.
        after = self._a_snapshot()
        before_hashes = {name: canonical_hash(value) for name, value in before.items()}
        after_hashes = {name: canonical_hash(value) for name, value in after.items()}
        differences = {name: {"before": before[name], "after": after[name]}
                       for name in before if before_hashes[name] != after_hashes[name]}
        self.assertEqual(before_hashes, after_hashes, json.dumps(differences, sort_keys=True, default=str))

        result = run_pass(self.conn, clock=lambda: self.now + timedelta(seconds=5))
        self.conn.commit()
        with self.conn.cursor() as cur:
            cur.execute("SELECT state,connector_status FROM source_liveness_current WHERE source_id=%s", (source_id,))
            live = cur.fetchone()
            self.assertEqual(live["state"], "ALIVE")
            cur.execute("SELECT freshness FROM series_freshness_current WHERE asset_id=%s AND metric_code='humidity'", (asset_id,))
            self.assertEqual(cur.fetchone()["freshness"], "MISSING")
            cur.execute("SELECT cause FROM data_gaps WHERE asset_id=%s AND metric_code='humidity' AND gap_end IS NULL", (asset_id,))
            self.assertEqual(cur.fetchone()["cause"], "NO_DATA")
            cur.execute("SELECT operational_state FROM asset_state_current WHERE asset_id=%s", (asset_id,))
            self.assertIsNotNone(cur.fetchone())

        equipment = self.client.get("/dashboard/equipment-health?hours=24", headers=self.headers)
        self.assertEqual(equipment.status_code, 200, equipment.text)
        b_rows = [row for row in equipment.json()["items"] if row["name"] == f"North Campus AHU {self.suffix}"]
        self.assertEqual(len(b_rows), 1)
        self.assertEqual(b_rows[0]["building"], building_name)
        overall = self.client.get("/dashboard/overview?hours=24", headers=self.headers)
        self.assertEqual(overall.status_code, 200, overall.text)
        b_freshness = self.client.get(f"/api/v1/freshness?building_code={building_code}&asset_code={asset_code}&limit=500",
                                      headers=self.headers).json()["items"]
        self.assertTrue(any(row["metric_code"] == "humidity" and row["freshness"] == "MISSING" for row in b_freshness))
        b_liveness = self.client.get(f"/api/v1/sources/liveness?building_code={building_code}&source_code={source_code}",
                                     headers=self.headers).json()["items"]
        self.assertEqual(len(b_liveness), 1)
        self.assertEqual(b_liveness[0]["state"], "ALIVE")
        b_gaps = self.client.get(f"/api/v1/data-gaps?building_code={building_code}&asset_code={asset_code}&status=open",
                                 headers=self.headers).json()["items"]
        self.assertTrue(any(row["metric_code"] == "humidity" and row["cause"] == "NO_DATA" for row in b_gaps))
        print(json.dumps({"event": "G4_A_BUILDING_PARITY", "hashes": after_hashes,
                          "building_b": building_code, "source": source_code,
                          "asset": asset_code, "freshness_pass": result}, sort_keys=True, default=str))


if __name__ == "__main__":
    unittest.main()
