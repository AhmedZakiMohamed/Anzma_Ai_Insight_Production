"""E4a gap lifecycle tests on a named disposable anzma_e4a_* database."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg

from app.adapters.database import db_adapter as da
from app.services.freshness import run_once


DATABASE = os.environ.get("ANZMA_E4A_TEST_DATABASE", "")
NOW = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=5)


class FreshnessGapLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATABASE.startswith("anzma_e4a_"):
            raise RuntimeError("ANZMA_E4A_TEST_DATABASE must name a disposable anzma_e4a_* database")
        if os.environ.get("PGHOST") != "127.0.0.1" or os.environ.get("PGPORT") != "25443":
            raise RuntimeError("set PGHOST=127.0.0.1 and PGPORT=25443 for the disposable database")
        cls.db = psycopg.connect(host="127.0.0.1", port=25443, dbname=DATABASE,
                                 user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""))
        cls.metric_id = cls.db.execute("SELECT metric_id FROM metric_types WHERE name='temperature'").fetchone()[0]
        cls.metric_code = "temperature"
        cls.db.commit()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def make_series(self, tag: str):
        suffix = uuid4().hex[:10]
        building = self.db.execute(
            "INSERT INTO buildings(name,building_code,timezone,work_start_local,work_end_local,non_working_weekdays) "
            "VALUES (%s,%s,'Africa/Cairo','08:00','18:00',ARRAY[5,6]::smallint[]) RETURNING building_id",
            (f"E4A {tag} {suffix}", f"E4A-BLD-{suffix}"),
        ).fetchone()[0]
        location = self.db.execute(
            "INSERT INTO locations(building_id,floor,name) VALUES (%s,0,%s) RETURNING location_id",
            (building, f"E4A {suffix}"),
        ).fetchone()[0]
        system_type = self.db.execute("SELECT system_type_id FROM system_types WHERE code='HVAC'").fetchone()[0]
        asset = self.db.execute(
            """INSERT INTO assets(asset_code,name,asset_type,system_type_id,location_id)
               VALUES (%s,%s,'ahu',%s,%s) RETURNING asset_id""",
            (f"E4A-{suffix}", f"E4A asset {suffix}", system_type, location),
        ).fetchone()[0]
        source = self.db.execute(
            """INSERT INTO data_sources(source_code,building_id,connector_name,connector_version)
               VALUES (%s,%s,'e4a-test','1') RETURNING source_id""",
            (f"E4A-{suffix}", building),
        ).fetchone()[0]
        expectation = self.db.execute(
            """INSERT INTO reporting_expectations(scope,asset_id,record_kind,metric_code,
                       expected_interval_seconds,stale_multiplier,missing_multiplier,priority,valid_from)
               VALUES ('ASSET_METRIC',%s,'telemetry','temperature',100,2,5,71,%s)
               RETURNING expectation_id""",
            (asset, NOW - timedelta(days=1)),
        ).fetchone()[0]
        self.db.commit()
        self.addCleanup(self.cleanup_series, building, location, asset, source, expectation)
        return {"building": building, "location": location, "asset": asset,
                "source": source, "expectation": expectation}

    def cleanup_series(self, building, location, asset, source, expectation):
        self.db.execute("DELETE FROM data_gap_cause_history WHERE gap_id IN (SELECT gap_id FROM data_gaps WHERE asset_id=%s)",
                        (asset,))
        self.db.execute("DELETE FROM data_gaps WHERE asset_id=%s", (asset,))
        self.db.execute("DELETE FROM series_freshness_current WHERE asset_id=%s", (asset,))
        self.db.execute("DELETE FROM telemetry_records WHERE asset_id=%s", (asset,))
        self.db.execute("DELETE FROM reporting_expectations WHERE expectation_id=%s", (expectation,))
        self.db.execute("DELETE FROM source_liveness_transitions WHERE source_id=%s", (source,))
        self.db.execute("DELETE FROM source_liveness_current WHERE source_id=%s", (source,))
        self.db.execute("DELETE FROM data_sources WHERE source_id=%s", (source,))
        self.db.execute("DELETE FROM assets WHERE asset_id=%s", (asset,))
        self.db.execute("DELETE FROM locations WHERE location_id=%s", (location,))
        self.db.execute("DELETE FROM buildings WHERE building_id=%s", (building,))
        self.db.commit()

    def add_reading(self, series, observed_at, quality="GOOD"):
        self.db.execute(
            """INSERT INTO telemetry_records(asset_id,metric_id,ts,value,source_id,received_at,quality,contract_version)
               VALUES (%s,%s,%s,22.5,%s,%s,%s,'v1')""",
            (series["asset"], self.metric_id, observed_at, series["source"], NOW, quality),
        )
        self.db.commit()

    def evaluate(self, now=NOW):
        conn = da.connect(host="127.0.0.1", port=25443, dbname=DATABASE,
                          user=os.environ["PGUSER"], password=os.environ.get("PGPASSWORD", ""))
        try:
            return run_once(conn, now)
        finally:
            conn.close()

    def gap_rows(self, series):
        return self.db.execute(
            """SELECT gap_id,gap_start,gap_end,expected_interval_seconds,detected_at,
                      opened_in_work_hours,cause,cause_updated_at
                 FROM data_gaps WHERE source_id=%s AND asset_id=%s AND record_kind='telemetry'
                   AND metric_code='temperature'""",
            (series["source"], series["asset"]),
        ).fetchall()

    def test_missing_opens_once_stale_does_not_and_db_enforces_one_open(self):
        series = self.make_series("open")
        last = NOW - timedelta(seconds=1000)
        self.add_reading(series, last)
        self.evaluate()
        rows = self.gap_rows(series)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1:5], (last, None, 100, NOW))
        self.assertFalse(rows[0][5], "the fixed clock is outside this building's local work hours")
        self.assertEqual(rows[0][6], "UNKNOWN")
        self.evaluate()
        self.assertEqual(len(self.gap_rows(series)), 1)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            with self.db.transaction():
                self.db.execute(
                    """INSERT INTO data_gaps(source_id,building_id,asset_id,record_kind,metric_code,
                               gap_start,opened_at,last_evaluated_at,expected_interval_seconds,detected_at,
                               opened_in_work_hours,cause,cause_updated_at)
                       VALUES (%s,%s,%s,'telemetry','temperature',%s,%s,%s,100,%s,false,'UNKNOWN',%s)""",
                    (series["source"], series["building"], series["asset"], last - timedelta(days=1),
                     NOW, NOW, NOW, NOW),
                )

    def test_gap_closes_only_on_newer_valid_observation_and_boundaries_stay_fixed(self):
        series = self.make_series("close")
        last = NOW - timedelta(seconds=1000)
        self.add_reading(series, last)
        self.evaluate()
        gap = self.gap_rows(series)[0]
        gap_id = gap[0]

        # A back-dated record inside the open interval is neither a close nor a split.
        self.add_reading(series, last - timedelta(seconds=500))
        self.evaluate()
        self.assertEqual(len(self.gap_rows(series)), 1)
        self.assertIsNone(self.gap_rows(series)[0][2])

        # BAD-quality data newer than gap_start is excluded by v_telemetry_valid.
        self.add_reading(series, last + timedelta(seconds=100), quality="BAD")
        self.evaluate()
        self.assertIsNone(self.gap_rows(series)[0][2])

        resumed = NOW + timedelta(seconds=100)
        self.add_reading(series, resumed)
        self.evaluate(now=NOW + timedelta(seconds=200))
        closed = self.db.execute("SELECT gap_start,gap_end,cause FROM data_gaps WHERE gap_id=%s", (gap_id,)).fetchone()
        self.assertEqual(closed, (last, resumed, "UNKNOWN"))

        # A back-dated row inside a closed interval cannot reopen, close, or split it.
        self.add_reading(series, last + timedelta(seconds=150))
        self.evaluate(now=NOW + timedelta(seconds=2))
        self.assertEqual(self.db.execute("SELECT gap_start,gap_end FROM data_gaps WHERE gap_id=%s", (gap_id,)).fetchone(),
                         (last, resumed))
        self.assertEqual(len(self.gap_rows(series)), 1)
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.db.transaction():
                self.db.execute("UPDATE data_gaps SET gap_start=gap_start-interval '1 second' WHERE gap_id=%s", (gap_id,))
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.db.transaction():
                self.db.execute("UPDATE data_gaps SET gap_end=gap_end+interval '1 second' WHERE gap_id=%s", (gap_id,))

    def test_stale_does_not_open_and_disabled_or_retired_series_do_not_open(self):
        stale_series = self.make_series("stale")
        self.add_reading(stale_series, NOW - timedelta(seconds=300))
        self.evaluate()
        self.assertEqual(self.db.execute(
            "SELECT freshness FROM series_freshness_current WHERE asset_id=%s", (stale_series["asset"],)
        ).fetchone()[0], "STALE")
        self.assertEqual(self.gap_rows(stale_series), [])

        retired = self.make_series("retired")
        self.db.execute("UPDATE assets SET lifecycle_status='RETIRED' WHERE asset_id=%s", (retired["asset"],))
        self.db.commit()
        self.add_reading(retired, NOW - timedelta(seconds=1000))
        self.evaluate()
        self.assertEqual(self.gap_rows(retired), [])

        disabled_asset = self.make_series("disabled-asset")
        self.db.execute("UPDATE assets SET lifecycle_status='DISABLED' WHERE asset_id=%s", (disabled_asset["asset"],))
        self.db.commit()
        self.add_reading(disabled_asset, NOW - timedelta(seconds=1000))
        self.evaluate()
        self.assertEqual(self.gap_rows(disabled_asset), [])

        disabled_source = self.make_series("disabled-source")
        self.db.execute("UPDATE data_sources SET status='DISABLED' WHERE source_id=%s", (disabled_source["source"],))
        self.db.commit()
        self.add_reading(disabled_source, NOW - timedelta(seconds=1000))
        self.evaluate()
        self.assertEqual(self.gap_rows(disabled_source), [])

    def test_open_gap_cause_is_mutable_and_closed_gap_cause_is_frozen(self):
        series = self.make_series("cause")
        last = NOW - timedelta(seconds=1000)
        self.add_reading(series, last)
        self.evaluate()
        gap_id = self.gap_rows(series)[0][0]
        self.db.execute("UPDATE data_gaps SET cause='NO_DATA' WHERE gap_id=%s", (gap_id,))
        cause, changed_at = self.db.execute(
            "SELECT cause,cause_updated_at FROM data_gaps WHERE gap_id=%s", (gap_id,)
        ).fetchone()
        self.assertEqual(cause, "NO_DATA")
        self.assertIsNotNone(changed_at)
        self.db.commit()
        self.add_reading(series, NOW + timedelta(seconds=10))
        self.evaluate(now=NOW + timedelta(seconds=20))
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.db.transaction():
                self.db.execute("UPDATE data_gaps SET cause='SOURCE_SUSPECTED' WHERE gap_id=%s", (gap_id,))

    def test_gap_is_evidence_only_and_does_not_create_offline_asset_state(self):
        series = self.make_series("evidence")
        self.add_reading(series, NOW - timedelta(seconds=1000))
        before = self.db.execute(
            "SELECT count(*) FROM equipment_state_records WHERE asset_id=%s", (series["asset"],)
        ).fetchone()[0]
        self.evaluate()
        after = self.db.execute(
            "SELECT count(*) FROM equipment_state_records WHERE asset_id=%s", (series["asset"],)
        ).fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM equipment_state_records WHERE asset_id=%s AND availability='UNAVAILABLE'",
            (series["asset"],),
        ).fetchone()[0], 0)
        self.assertEqual(len(self.gap_rows(series)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
