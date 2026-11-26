from __future__ import annotations

import unittest
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import alert_stream_api


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.query = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.query, self.params = query, params
        if "MAX(alert_id)" in query:
            return Result([{"latest_event_id": self.rows[-1]["id"] if self.rows else 0}])
        return Result(self.rows)


def alert_row(alert_id):
    now = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)
    return {
        "id": alert_id, "rule_id": "HVAC_HIGH_TEMP", "detection_method": "threshold",
        "severity": "warning", "status": "open", "title": "High temperature",
        "summary": "Fixture alert", "first_ts": now, "last_ts": now, "detected_ts": now,
        "incident_id": None, "asset_id": 1, "asset_name": "AHU-1", "zone": "North",
        "system": "HVAC",
    }


class AlertStreamAPITests(unittest.TestCase):
    def client_for(self, rows):
        self.connection = FakeConnection(rows)
        app = FastAPI()
        app.include_router(alert_stream_api.create_router(lambda: self.connection))
        return TestClient(app)

    def test_replay_is_oldest_first_and_advances_cursor(self):
        client = self.client_for([alert_row(4), alert_row(5)])
        response = client.get("/api/v1/alerts/stream", params={"after_id": 3, "limit": 1})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["after_id"], 3)
        self.assertEqual(body["next_after_id"], 4)
        self.assertTrue(body["has_more"])
        self.assertEqual([item["id"] for item in body["items"]], [4])
        self.assertIn("al.alert_id > %s ORDER BY al.alert_id ASC LIMIT %s", self.connection.query)
        self.assertEqual(self.connection.params, (3, 2))

    def test_empty_replay_keeps_the_supplied_cursor(self):
        response = self.client_for([]).get("/api/v1/alerts/stream", params={"after_id": 99})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": [], "after_id": 99, "next_after_id": 99, "has_more": False})

    def test_latest_route_returns_global_watermark(self):
        response = self.client_for([alert_row(17)]).get("/api/v1/alerts/stream/latest")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"latest_event_id": 17})

    def test_cursor_and_page_size_are_bounded(self):
        client = self.client_for([])
        self.assertEqual(client.get("/api/v1/alerts/stream", params={"after_id": -1}).status_code, 422)
        self.assertEqual(client.get("/api/v1/alerts/stream", params={"limit": 501}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
