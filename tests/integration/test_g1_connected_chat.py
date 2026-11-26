"""G1 chat/API acceptance checks against an already-ingested synthetic fixture.

Run with ANZMA_TEST_CORE_URL and ANZMA_TEST_OPERATOR_TOKEN pointed at an isolated,
disposable Core. This suite makes HTTP requests only; it never connects to PostgreSQL.
"""
from __future__ import annotations

import json
import os
import unittest
import urllib.error
import urllib.request


CORE_URL = os.getenv("ANZMA_TEST_CORE_URL", "").rstrip("/")
OPERATOR_TOKEN = os.getenv("ANZMA_TEST_OPERATOR_TOKEN", "")


@unittest.skipUnless(CORE_URL and OPERATOR_TOKEN, "requires an isolated G1 Core URL and operator token")
class G1ConnectedChatAcceptance(unittest.TestCase):
    def post(self, path: str, body: dict, *, authenticated: bool = True) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + OPERATOR_TOKEN
        request = urllib.request.Request(CORE_URL + path,
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            return exc.code, payload

    def test_operator_authentication_is_required(self):
        status, body = self.post("/api/v1/analytics/execute", {
            "analysis": "doors.after_hours", "start_date": "2026-11-01", "end_date": "2026-11-07",
        }, authenticated=False)
        self.assertEqual(status, 401, body)

    def test_after_hours_chat_matches_typed_result_and_fixture(self):
        request = {"analysis": "doors.after_hours", "start_date": "2026-11-01",
                   "end_date": "2026-11-07", "building_code": "MAIN-TOWER", "after_local": "20:00:00"}
        status, analytic = self.post("/api/v1/analytics/execute", request)
        self.assertEqual(status, 200, analytic)
        status, chat = self.post("/chat", {"message":
            "Which doors physically opened after 8 PM from 2026-11-01 through 2026-11-07?", "use_llm": False})
        self.assertEqual(status, 200, chat)
        expected_ids = {"evt-door-001", "evt-door-003"}
        analytic_ids = {ref["source_event_id"] for row in analytic["rows"] for ref in row["evidence_refs"]}
        chat_ids = {ref["source_event_id"] for row in chat["result"]["rows"] for ref in row["evidence_refs"]}
        self.assertEqual(analytic["total_rows"], 2)
        self.assertEqual(analytic_ids, expected_ids)
        self.assertEqual(chat["result"]["total_rows"], analytic["total_rows"])
        self.assertEqual(chat_ids, analytic_ids)
        self.assertTrue(all(row["observed_at_local"] >= "2026-11-01T20:00:00" for row in analytic["rows"]))
        self.assertEqual(chat["result"]["completeness"], "UNKNOWN")
        self.assertFalse(chat["data_sufficient"])

    def test_offline_ranking_counts_outages_once_and_cites_start_events(self):
        request = {"analysis": "devices.offline_frequency", "start_date": "2026-11-01",
                   "end_date": "2026-11-07", "building_code": "MAIN-TOWER"}
        status, analytic = self.post("/api/v1/analytics/execute", request)
        self.assertEqual(status, 200, analytic)
        status, chat = self.post("/chat", {"message":
            "Which devices had the highest number of offline incidents from 2026-11-01 through 2026-11-07?",
            "use_llm": False})
        self.assertEqual(status, 200, chat)
        expected = {"HVAC-AHU-01": (1, "evt-offline-001"), "NET-SW-01": (1, "evt-offline-004")}
        actual = {row["asset_code"]: (row["offline_incidents"],
                   row["evidence_refs"][0]["source_event_id"]) for row in analytic["rows"]}
        chat_actual = {row["asset_code"]: (row["offline_incidents"],
                         row["evidence_refs"][0]["source_event_id"]) for row in chat["result"]["rows"]}
        self.assertEqual(actual, expected)
        self.assertEqual(chat_actual, actual)
        all_refs = {ref["source_event_id"] for row in chat["result"]["rows"] for ref in row["evidence_refs"]}
        self.assertNotIn("evt-offline-002", all_refs)
        self.assertEqual(chat["result"]["completeness"], "UNKNOWN")
        self.assertFalse(chat["data_sufficient"])


if __name__ == "__main__":
    unittest.main()
