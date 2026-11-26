from __future__ import annotations

import os
import json
import threading
import tempfile
import unittest
from datetime import date, datetime, time, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from connectors.reference_rest import connector, server
from connectors.reference_rest.checkpoint import SyncCheckpointStore
from connectors.sdk import ConnectorLifecycle, EventHistoryConnector


class ReferenceRESTHTTPTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"REFERENCE_API_TOKEN": "local-test-secret"})
        self.env.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.env.stop()

    def test_authenticated_http_pagination_returns_complete_history(self):
        with patch.object(connector, "PAGE_LIMIT", 2):
            events, complete = connector.fetch_events(
                self.base_url, "local-test-secret", "2026-11-01T00:00:00Z", "2026-11-07T00:00:00Z")
        self.assertTrue(complete)
        self.assertEqual(len(events), 8)
        self.assertEqual(events[0].external_event_id, "evt-door-001")
        self.assertEqual(events[-1].external_event_id, "evt-offline-005")

    def test_missing_auth_is_rejected(self):
        with self.assertRaises(connector.ConnectorError) as caught:
            connector.fetch_events(self.base_url, "wrong-secret",
                                   "2026-11-01T00:00:00Z", "2026-11-07T00:00:00Z")
        self.assertIn("HTTP 401", str(caught.exception))

    def test_reference_adapter_implements_lifecycle_and_event_history_contracts(self):
        adapter = connector.ReferenceRESTConnector(self.base_url, "local-test-secret")
        self.assertIsInstance(adapter, ConnectorLifecycle)
        self.assertIsInstance(adapter, EventHistoryConnector)
        self.assertTrue(adapter.test_connection().ok)
        self.assertEqual(adapter.health().status, "CONNECTED")

    def test_sdk_events_map_through_ingestion_contract(self):
        events, complete = connector.fetch_events(
            self.base_url, "local-test-secret", "2026-11-01T00:00:00Z", "2026-11-07T00:00:00Z")
        self.assertTrue(complete)
        batch = connector.canonical_batch(events, "reference", "MAIN-TOWER")
        self.assertEqual(batch["contract_version"], "1.0")
        self.assertEqual(batch["records"][0]["source_event_id"], "evt-door-001")
        self.assertEqual(batch["records"][0]["event_type"], "door_opened")

    def test_incomplete_intermediate_page_keeps_entire_history_incomplete(self):
        first = {"id": "evt-1", "asset_external_id": "door-1", "event_type": "DOOR_OPENED",
                 "observed_at": "2026-11-02T18:00:00Z", "details": {}}
        second = {"id": "evt-2", "asset_external_id": "door-1", "event_type": "DOOR_OPENED",
                  "observed_at": "2026-11-02T18:01:00Z", "details": {}}
        pages = [
            {"items": [first], "next_cursor": "page-2", "complete": False},
            {"items": [second], "next_cursor": None, "complete": True},
        ]
        with patch.object(connector, "_request", side_effect=pages):
            events, complete = connector.fetch_events(
                self.base_url, "local-test-secret", "2026-11-01T00:00:00Z", "2026-11-07T00:00:00Z")
        self.assertEqual([item.external_event_id for item in events], ["evt-1", "evt-2"])
        self.assertFalse(complete)

    def test_missing_page_completeness_metadata_is_not_assumed_complete(self):
        with patch.object(connector, "_request", return_value={"items": [], "next_cursor": None}):
            with self.assertRaisesRegex(connector.ConnectorError, "completeness metadata"):
                connector.fetch_events(
                    self.base_url, "local-test-secret", "2026-11-01T00:00:00Z", "2026-11-07T00:00:00Z")


class ConnectorMappingTests(unittest.TestCase):
    def test_access_grant_remains_distinct_from_physical_open(self):
        events = [
            {"id": "open-1", "asset_external_id": "door-1", "event_type": "DOOR_OPENED",
             "observed_at": "2026-11-02T18:00:00Z", "details": {"contact": "open"}},
            {"id": "grant-1", "asset_external_id": "door-1", "event_type": "ACCESS_GRANTED",
             "observed_at": "2026-11-02T18:01:00Z", "details": {}},
        ]
        batch = connector.canonical_batch(events, "reference", "HQ")
        self.assertEqual([row["event_type"] for row in batch["records"]], ["door_opened", "access_granted"])
        self.assertNotEqual(batch["records"][0]["event_type"], batch["records"][1]["event_type"])

    def test_unknown_type_fails_instead_of_silently_dropping_source_data(self):
        with self.assertRaises(connector.ConnectorError):
            connector.canonical_batch([{"event_type": "UNMAPPED", "id": "x"}], "reference", "HQ")

    def test_non_utc_timestamp_is_rejected(self):
        event = {"id": "open-1", "asset_external_id": "door-1", "event_type": "DOOR_OPENED",
                 "observed_at": "2026-11-02T20:00:00+02:00", "details": {}}
        with self.assertRaises(connector.ConnectorError):
            connector.canonical_batch([event], "reference", "HQ")

    def test_core_partial_batch_is_not_reported_as_success(self):
        with self.assertRaisesRegex(connector.ConnectorError, "source sync is incomplete"):
            connector.require_full_acceptance({"batch_id": "batch-1", "status": "PARTIAL",
                                               "counts": {"received": 8, "accepted": 7,
                                                          "duplicate": 0, "rejected": 1}})

    def test_core_fully_accepted_batch_is_success(self):
        connector.require_full_acceptance({"batch_id": "batch-2", "status": "ACCEPTED",
                                            "counts": {"received": 8, "accepted": 8,
                                                       "duplicate": 0, "rejected": 0}})


class CheckpointedSyncTests(unittest.TestCase):
    START = "2026-11-01T00:00:00Z"
    END = "2026-11-07T00:00:00Z"

    def _event(self, event_id):
        from connectors.sdk import CanonicalEvent
        return CanonicalEvent(event_id, "door-1", "door_opened", "2026-11-02T18:00:00Z")

    def test_lost_response_replays_identical_batch_before_advancing_cursor(self):
        first = connector.Page((self._event("event-1"),), "page-2", True)
        second = connector.Page((self._event("event-2"),), None, True)
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            store = SyncCheckpointStore(state_path)
            adapter = connector.ReferenceRESTConnector("http://example.test", "token")
            sent = []

            def lose_first_response(_url, _token, batch):
                sent.append(batch)
                raise connector.ConnectorError("simulated response lost after Core commit")

            with patch.object(adapter, "read_events", return_value=first):
                with self.assertRaisesRegex(connector.ConnectorError, "response lost"):
                    connector.run_checkpointed_sync(adapter, store, "source", "HQ", self.START,
                                                    self.END, "http://core.test", "key", push=lose_first_response)
            pending = SyncCheckpointStore(state_path).state("source", "HQ")
            self.assertIsNone(pending["next_cursor"])
            self.assertEqual(pending["pending_next_cursor"], "page-2")
            self.assertEqual(pending["pending_batch"]["batch_id"], sent[0]["batch_id"])

            def accept(_url, _token, batch):
                sent.append(batch)
                return {"batch_id": batch["batch_id"], "status": "ACCEPTED",
                        "counts": {"received": len(batch["records"]), "accepted": len(batch["records"]),
                                   "duplicate": 0, "rejected": 0}}

            resumed_store = SyncCheckpointStore(state_path)
            with patch.object(adapter, "read_events", return_value=second) as read_page:
                result = connector.run_checkpointed_sync(adapter, resumed_store, "source", "HQ", self.START,
                                                         self.END, "http://core.test", "key", push=accept)
            self.assertEqual(sent[0], sent[1])
            self.assertEqual(read_page.call_count, 1)
            self.assertEqual(result["status"], "COMPLETE")
            self.assertEqual(result["page_count"], 2)
            self.assertEqual(resumed_store.state("source", "HQ")["status"], "COMPLETE")

    def test_checkpoint_store_rejects_concurrent_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SyncCheckpointStore(Path(directory) / "state.sqlite3")
            with store.exclusive_run():
                with self.assertRaisesRegex(RuntimeError, "another connector run is active"):
                    with store.exclusive_run():
                        pass

    def test_completed_range_is_not_fetched_or_submitted_again(self):
        page = connector.Page((self._event("event-1"),), None, True)
        with tempfile.TemporaryDirectory() as directory:
            store = SyncCheckpointStore(Path(directory) / "state.sqlite3")
            adapter = connector.ReferenceRESTConnector("http://example.test", "token")
            accept = lambda _url, _token, batch: {
                "batch_id": batch["batch_id"], "status": "ACCEPTED",
                "counts": {"received": 1, "accepted": 1, "duplicate": 0, "rejected": 0}}
            with patch.object(adapter, "read_events", return_value=page):
                connector.run_checkpointed_sync(adapter, store, "source", "HQ", self.START,
                                                self.END, "http://core.test", "key", push=accept)
            with patch.object(adapter, "read_events") as read_page:
                result = connector.run_checkpointed_sync(adapter, store, "source", "HQ", self.START,
                                                         self.END, "http://core.test", "key", push=accept)
            read_page.assert_not_called()
            self.assertTrue(result["skipped"])
            self.assertEqual(result["status"], "COMPLETE")

    def test_incomplete_page_never_marks_the_range_complete(self):
        page = connector.Page((self._event("event-1"),), None, False)
        with tempfile.TemporaryDirectory() as directory:
            store = SyncCheckpointStore(Path(directory) / "state.sqlite3")
            adapter = connector.ReferenceRESTConnector("http://example.test", "token")
            submitted = []
            accept = lambda _url, _token, batch: {
                "batch_id": batch["batch_id"], "status": "ACCEPTED",
                "counts": {"received": 1, "accepted": 1, "duplicate": 0, "rejected": 0}}
            def capture(_url, _token, batch):
                submitted.append(batch)
                return accept(_url, _token, batch)
            with patch.object(adapter, "read_events", return_value=page):
                result = connector.run_checkpointed_sync(adapter, store, "source", "HQ", self.START,
                                                         self.END, "http://core.test", "key", push=capture)
            self.assertEqual(result["status"], "INCOMPLETE")
            self.assertFalse(result["complete"])
            self.assertNotEqual(store.state("source", "HQ")["status"], "COMPLETE")
            self.assertFalse(submitted[0]["source"]["connector_status"]["upstream_page_complete"])

    def test_source_fixture_matches_documented_golden_event_ids_and_counts(self):
        fixture_dir = Path(server.__file__).with_name("fixture")
        events = json.loads((fixture_dir / "events.json").read_text(encoding="utf-8"))
        expected = json.loads((fixture_dir / "expectations.json").read_text(encoding="utf-8"))
        zone = ZoneInfo(expected["fixture_timezone"])
        start = date.fromisoformat(expected["period"]["start_date"])
        end = date.fromisoformat(expected["period"]["end_date"])
        threshold = time.fromisoformat(expected["period"]["after_local"])
        doors = [event for event in events if event["event_type"] == "DOOR_OPENED"
                 and start <= datetime.fromisoformat(event["observed_at"].replace("Z", "+00:00")).astimezone(zone).date() <= end
                 and datetime.fromisoformat(event["observed_at"].replace("Z", "+00:00")).astimezone(zone).time() >= threshold]
        self.assertEqual(sorted(event["id"] for event in doors),
                         sorted(expected["doors_after_hours"]["included_source_event_ids"]))
        self.assertEqual(len(doors), expected["doors_after_hours"]["event_count"])
        self.assertIn("evt-door-002", expected["doors_after_hours"]["excluded_event_ids"])

        active, counts = {}, {}
        for event in sorted((event for event in events if event["event_type"] in {"OFFLINE_START", "OFFLINE_END"}),
                            key=lambda item: (item["asset_external_id"], item["observed_at"], item["id"])):
            asset = event["asset_external_id"]
            if event["event_type"] == "OFFLINE_START":
                if not active.get(asset, False):
                    counts.setdefault(asset, []).append(event["id"])
                    active[asset] = True
            else:
                active[asset] = False
        actual_rank = [{"asset_external_id": asset, "offline_incidents": len(ids), "offline_start_ids": ids}
                       for asset, ids in sorted(counts.items(), key=lambda item: (-len(item[1]), item[0]))]
        self.assertEqual(actual_rank, expected["offline_frequency"]["ranked_assets"])


class FakeCursor:
    def __init__(self, result_sets):
        self.result_sets = list(result_sets)
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return self.result_sets.pop(0)


class OperationalAnalyticsTests(unittest.TestCase):
    def setUp(self):
        from app.services.analytics import operational
        self.analytics = operational
        self.buildings = [{"building_code": "HQ", "timezone": "Africa/Cairo"}]

    def test_doors_result_has_local_and_source_evidence(self):
        cursor = FakeCursor([self.buildings, [{
            "event_id": 41, "source_event_id": "door-open-41",
            "ts": datetime(2026, 11, 2, 18, 30, tzinfo=timezone.utc),
            "local_ts": datetime(2026, 11, 2, 20, 30), "timezone": "Africa/Cairo",
            "building_code": "HQ", "building_name": "HQ Building", "asset_code": "D-1",
            "asset_name": "North Door", "location_name": "North", "source_code": "reference",
            "result_total": 1,
        }]])
        result = self.analytics.doors_after_hours(cursor, datetime(2026, 11, 1).date(),
            datetime(2026, 11, 7).date())
        self.assertEqual(result["total_rows"], 1)
        self.assertEqual(result["rows"][0]["observed_at_local"], "2026-11-02T20:30:00")
        self.assertEqual(result["rows"][0]["evidence_refs"][0]["source_event_id"], "door-open-41")
        self.assertEqual(result["source_ids"], ["reference"])

    def test_repeated_offline_start_counts_once_until_explicit_recovery(self):
        start = {"asset_id": 7, "event_type": "offline_start", "in_window": False,
                 "event_id": 1, "source_event_id": "start-before", "source_code": "reference",
                 "building_code": "HQ", "asset_code": "AHU-3", "asset_name": "AHU 3"}
        repeated = {**start, "in_window": True, "event_id": 2, "source_event_id": "repeat"}
        recovery = {**start, "in_window": True, "event_type": "offline_end", "event_id": 3,
                    "source_event_id": "recovery"}
        next_outage = {**start, "in_window": True, "event_id": 4, "source_event_id": "start-again"}
        cursor = FakeCursor([self.buildings, [start, repeated, recovery, next_outage]])
        result = self.analytics.offline_frequency(cursor, datetime(2026, 11, 1).date(),
            datetime(2026, 11, 7).date())
        row = result["rows"][0]
        self.assertEqual(row["offline_incidents"], 1)
        self.assertEqual([ref["source_event_id"] for ref in row["evidence_refs"]], ["start-again"])

    def test_period_over_366_calendar_days_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "366 calendar days"):
            self.analytics.offline_frequency(None, datetime(2025, 1, 1).date(), datetime(2026, 1, 2).date())


if __name__ == "__main__":
    unittest.main()
