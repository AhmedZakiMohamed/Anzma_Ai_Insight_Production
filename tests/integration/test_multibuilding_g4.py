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


EXPECTED_PROJECT = "anzma-g4-proof-final-20261123-1542"
if os.getenv("ANZMA_G4_DISPOSABLE_PROJECT") != EXPECTED_PROJECT or os.getenv("PGHOST") != "db":
    raise RuntimeError(f"Run only with ANZMA_G4_DISPOSABLE_PROJECT={EXPECTED_PROJECT} inside its compose API container")

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
        return self._public_snapshot("MAIN-TOWER")

    def _public_snapshot(self, building_code=None):
        paths = {
            "overview": "/dashboard/overview?hours=24",
            "trends": "/dashboard/trends?hours=24",
            "equipment_health": "/dashboard/equipment-health?hours=24",
            "alerts": "/alerts?limit=500",
            "incidents": "/incidents",
            "freshness": "/api/v1/freshness?limit=500",
            "liveness": "/api/v1/sources/liveness?limit=500",
            "gaps": "/api/v1/data-gaps?status=all&limit=500",
        }
        result = {}
        for name, path in paths.items():
            url = f"{path}{'&' if '?' in path else '?'}building={building_code}" if building_code else path
            response = self.client.get(url, headers=self.headers)
            self.assertEqual(response.status_code, 200, f"{url}: {response.text}")
            result[name] = response.json()
        return result

    def _hashes(self, snapshot):
        return {name: canonical_hash(value) for name, value in snapshot.items()}

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
        legacy_hashes = {
            "overview": "27c928ddd0a5d973c28615bfa0bd7ac87acdb0357b026c0d00a8b5b490319f45",
            "trends": "d46f8bb08576fc5875e1f018a70e2c8c2387e2907cc20d20f8a47b16585aad6b",
            "equipment_health": "7d5e43801123242608be62f505424a61d29ee2be02c2f2d09743e17c0778d211",
            "alerts": "bd712f94c21c3aa2e4d07520b64c4ecedc56301f98379112c79cdb3e8fe56f31",
            "incidents": "e535715f04075f62a60beaf5e39a851c7de593c8b8e7ffedbcf01ae18e819ff7",
            "freshness": "aaaca23ffcb2f8de0dffafc97338bf2736e89ba45617b2611661dad3e193d33b",
            "liveness": "6a6031629b625e97621af9617a2302153c9794f73475a6d171743ff04c73fbbf",
            "gaps": "d68c75a3e9047e46f1cb6486f7fd79e29cd1478a1ce50ea429b160614cd6f7fa",
        }
        before_all = self._public_snapshot()
        self.assertEqual(self._hashes(before_all), legacy_hashes,
                         "omitted building filter must preserve pre-change public responses")
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

        observation_end = self.window_end - timedelta(seconds=30)

        def stamp(value):
            return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

        def envelope(records):
            return {"contract_version": "1.0", "batch_id": str(uuid4()), "sent_at": stamp(self.now),
                    "source": {"source_code": source_code, "connector": {"name": "g4-proof", "version": "1.0"},
                               "connector_status": {"vendor_link": "UP", "buffered_records": 0}},
                    "building_code": building_code, "records": records}

        headers = {"Authorization": f"Bearer {key}"}
        temperature_start = self.window_end - timedelta(hours=1)
        records = [
            {"record_type": "telemetry", "asset": {"external_id": external_id}, "metric_code": "temperature",
             "value": 32.0, "unit": "C", "observed_at": stamp(temperature_start)},
            {"record_type": "telemetry", "asset": {"external_id": external_id}, "metric_code": "temperature",
             "value": 34.0, "unit": "C", "observed_at": stamp(temperature_start + timedelta(minutes=15))},
            {"record_type": "telemetry", "asset": {"external_id": external_id}, "metric_code": "temperature",
             "value": 35.0, "unit": "C", "observed_at": stamp(temperature_start + timedelta(minutes=30))},
            {"record_type": "telemetry", "asset": {"external_id": external_id}, "metric_code": "humidity",
             "value": 44.0, "unit": "%", "observed_at": stamp(observation_end - timedelta(hours=3))},
            {"record_type": "event", "source_event_id": f"event-{self.suffix}", "asset": {"external_id": external_id},
             "event_type": "hvac_fault", "severity": "CRITICAL", "observed_at": stamp(observation_end - timedelta(minutes=1))},
            {"record_type": "state", "asset": {"external_id": external_id}, "observed_at": stamp(observation_end),
             "reported_state": "RUNNING", "availability": "AVAILABLE"},
        ]
        accepted = self.client.post("/api/v1/ingest/batches", json=envelope(records), headers=headers)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["counts"]["accepted"], 6)
        keepalive = self.client.post("/api/v1/ingest/batches", json=envelope([]), headers=headers)
        self.assertEqual(keepalive.status_code, 200, keepalive.text)
        self.assertEqual(keepalive.json()["counts"]["received"], 0)

        foreign = {"record_type": "telemetry", "asset": {"asset_code": "HVAC-AHU-01"},
                   "metric_code": "temperature", "value": 21.0, "unit": "C", "observed_at": stamp(observation_end)}
        denied = self.client.post("/api/v1/ingest/batches", json=envelope([foreign]), headers=headers)
        self.assertEqual(denied.status_code, 422)
        self.assertEqual(denied.json()["rejected"][0]["reason_code"], "ASSET_NOT_PERMITTED")

        # Compare A immediately after B's ingest and before the global job refreshes
        # its evaluation timestamps for every building.
        after = self._a_snapshot()
        before_hashes = self._hashes(before)
        after_hashes = self._hashes(after)
        differences = {name: {"before": before[name], "after": after[name]}
                       for name in before if before_hashes[name] != after_hashes[name]}
        self.assertEqual(before_hashes, after_hashes, json.dumps(differences, sort_keys=True, default=str))

        detected = self.client.post("/alerts/run", headers=self.headers)
        self.assertEqual(detected.status_code, 200, detected.text)
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

        equipment = self.client.get(f"/dashboard/equipment-health?hours=24&building={building_code}", headers=self.headers)
        self.assertEqual(equipment.status_code, 200, equipment.text)
        b_rows = [row for row in equipment.json()["items"] if row["name"] == f"North Campus AHU {self.suffix}"]
        self.assertEqual(len(b_rows), 1)
        self.assertEqual(b_rows[0]["building"], building_name)
        self.assertEqual(len(equipment.json()["items"]), 1)
        overview = self.client.get(f"/dashboard/overview?hours=24&building={building_code}", headers=self.headers)
        self.assertEqual(overview.status_code, 200, overview.text)
        self.assertEqual(sum(item["assets"] for item in overview.json()["systems"]), 1)
        self.assertEqual(next(item["value"] for item in overview.json()["kpis"]
                              if item["key"] == "monitored_assets"), 1)
        trends = self.client.get(f"/dashboard/trends?hours=24&building={building_code}", headers=self.headers)
        self.assertEqual(trends.status_code, 200, trends.text)
        self.assertEqual(trends.json()["top_assets"]["labels"], [f"North Campus AHU {self.suffix}"])
        b_alerts = self.client.get(f"/alerts?limit=500&building={building_code}", headers=self.headers)
        self.assertEqual(b_alerts.status_code, 200, b_alerts.text)
        self.assertTrue(b_alerts.json()["items"])
        self.assertTrue(all(item["asset_id"] == asset_id for item in b_alerts.json()["items"]))
        b_incidents = self.client.get(f"/incidents?building={building_code}", headers=self.headers)
        self.assertEqual(b_incidents.status_code, 200, b_incidents.text)
        self.assertTrue(b_incidents.json()["incidents"])
        self.assertTrue(all(item["building"] == building_name for item in b_incidents.json()["incidents"]))
        overall = self.client.get("/dashboard/overview?hours=24", headers=self.headers)
        self.assertEqual(overall.status_code, 200, overall.text)
        self.assertEqual(sum(item["assets"] for item in overall.json()["systems"]), 13)
        all_health = self.client.get("/dashboard/equipment-health?hours=24", headers=self.headers).json()
        self.assertTrue(any(row["name"] == f"North Campus AHU {self.suffix}" for row in all_health["items"]))
        all_trends = self.client.get("/dashboard/trends?hours=24", headers=self.headers).json()
        old_event_count = sum(sum(values) for values in before_all["trends"]["events_by_system"].values())
        all_event_count = sum(sum(values) for values in all_trends["events_by_system"].values())
        self.assertEqual(all_event_count, old_event_count + 1)
        all_alerts = self.client.get("/alerts?limit=500", headers=self.headers).json()["items"]
        self.assertTrue(any(row["asset_id"] == asset_id for row in all_alerts))
        all_incidents = self.client.get("/incidents", headers=self.headers).json()["incidents"]
        self.assertTrue(any(row["building"] == building_name for row in all_incidents))
        all_freshness = self.client.get("/api/v1/freshness?limit=500", headers=self.headers).json()["items"]
        self.assertTrue(any(row["building_code"] == building_code for row in all_freshness))
        all_liveness = self.client.get("/api/v1/sources/liveness?limit=500", headers=self.headers).json()["items"]
        self.assertTrue(any(row["building_code"] == building_code for row in all_liveness))
        all_gaps = self.client.get("/api/v1/data-gaps?status=all&limit=500", headers=self.headers).json()["items"]
        self.assertTrue(any(row["building_code"] == building_code for row in all_gaps))
        b_freshness = self.client.get(f"/api/v1/freshness?building={building_code}&asset_code={asset_code}&limit=500",
                                      headers=self.headers).json()["items"]
        self.assertTrue(any(row["metric_code"] == "humidity" and row["freshness"] == "MISSING" for row in b_freshness))
        b_liveness = self.client.get(f"/api/v1/sources/liveness?building={building_code}&source_code={source_code}",
                                     headers=self.headers).json()["items"]
        self.assertEqual(len(b_liveness), 1)
        self.assertEqual(b_liveness[0]["state"], "ALIVE")
        b_gaps = self.client.get(f"/api/v1/data-gaps?building={building_code}&asset_code={asset_code}&status=open",
                                 headers=self.headers).json()["items"]
        self.assertTrue(any(row["metric_code"] == "humidity" and row["cause"] == "NO_DATA" for row in b_gaps))
        for path in ("/dashboard/overview", "/dashboard/trends", "/dashboard/equipment-health",
                     "/alerts", "/incidents", "/api/v1/freshness", "/api/v1/sources/liveness",
                     "/api/v1/data-gaps"):
            query = "?building=G4-NO-SUCH-CODE"
            response = self.client.get(path + query, headers=self.headers)
            self.assertEqual(response.status_code, 400, f"{path}: {response.status_code} {response.text}")
        options = self.client.get("/dashboard/filters", headers=self.headers)
        self.assertEqual(options.status_code, 200, options.text)
        self.assertIn(building_code, {item["building_code"] for item in options.json()["buildings"]})
        dashboard = self.client.get("/dashboard", headers=self.headers)
        self.assertEqual(dashboard.status_code, 200, dashboard.text)
        self.assertIn('id="buildingFilter"', dashboard.text)
        self.assertIn("All buildings", dashboard.text)
        print(json.dumps({"event": "G4_PUBLIC_API_BUILDING_FILTERS", "legacy_no_filter_hashes": legacy_hashes,
                          "a_before_after_hashes": after_hashes,
                          "building_b": building_code, "source": source_code,
                          "asset": asset_code, "freshness_pass": result}, sort_keys=True, default=str))


if __name__ == "__main__":
    unittest.main()
