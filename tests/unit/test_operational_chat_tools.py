from __future__ import annotations

import unittest
from unittest.mock import patch

from app.api import chat_lite
from app.domain.ai.model_provider import ModelReply, ToolCall


class FakeProvider:
    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.reply


def door_result():
    return {
        "result_id": "res_test", "tool": "doors.after_hours", "generated_at": "2026-11-08T12:00:00Z",
        "total_rows": 1, "truncated": False, "limitations": [],
        "rows": [{"building_code": "HQ", "asset_code": "D-1", "door": "North Door",
                   "evidence_refs": [{"event_id": 7, "source_event_id": "evt-7", "source_code": "reference"}]}],
    }


class OperationalChatToolTests(unittest.TestCase):
    def test_model_selects_only_typed_tool_and_core_calculates_result(self):
        provider = FakeProvider(ModelReply(tool_calls=(ToolCall("analyze_after_hours_doors", {
            "start_date": "2026-11-01", "end_date": "2026-11-07", "building_code": "HQ",
        }),)))
        with patch("app.services.analytics.operational.doors_after_hours", return_value=door_result()) as run:
            response = chat_lite._operational_query(
                object(), "Which doors physically opened after 8:30 PM from 2026-11-01 through 2026-11-07?", "en",
                model_provider=provider, use_llm=True)
        self.assertEqual(response["tool_routing"], "MODEL_TOOL")
        self.assertEqual(response["result"]["result_id"], "res_test")
        self.assertEqual(response["sources"][0]["text"].split("source_event_id=")[1].split(";")[0], "evt-7")
        self.assertEqual(run.call_args.kwargs["building_code"], "HQ")
        self.assertEqual(run.call_args.kwargs["after_local"].isoformat(timespec="minutes"), "20:30")
        self.assertEqual([tool["name"] for tool in provider.calls[0]["tools"]],
                         ["analyze_after_hours_doors", "rank_offline_devices"])

    def test_unavailable_model_uses_explicit_date_deterministic_fallback(self):
        provider = FakeProvider(error=TimeoutError("model unavailable"))
        with patch("app.services.analytics.operational.doors_after_hours", return_value=door_result()) as run:
            response = chat_lite._operational_query(
                object(), "Which doors opened from 2026-11-01 through 2026-11-07?", "en",
                model_provider=provider, use_llm=True)
        self.assertEqual(response["tool_routing"], "DETERMINISTIC_FALLBACK")
        self.assertEqual(run.call_args.args[1].isoformat(), "2026-11-01")
        self.assertEqual(run.call_args.args[2].isoformat(), "2026-11-07")

    def test_model_cannot_select_another_analysis_or_supply_extra_arguments(self):
        provider = FakeProvider(ModelReply(tool_calls=(ToolCall("rank_offline_devices", {
            "start_date": "2026-11-01", "end_date": "2026-11-07", "sql": "SELECT * FROM event_records",
        }),)))
        with patch("app.services.analytics.operational.doors_after_hours", return_value=door_result()):
            response = chat_lite._operational_query(
                object(), "Which doors opened from 2026-11-01 through 2026-11-07?", "en",
                model_provider=provider, use_llm=True)
        self.assertEqual(response["tool_routing"], "DETERMINISTIC_FALLBACK")
        self.assertNotIn("SELECT", response["answer"])

    def test_model_cannot_invent_missing_dates(self):
        provider = FakeProvider(ModelReply(tool_calls=(ToolCall("analyze_after_hours_doors", {
            "start_date": "2026-11-01", "end_date": "2026-11-07",
        }),)))
        with patch("app.services.analytics.operational.doors_after_hours") as run:
            response = chat_lite._operational_query(
                object(), "Which doors opened after 8 PM?", "en", model_provider=provider, use_llm=True)
        self.assertFalse(response["grounding"]["passed"])
        self.assertIn("start and end date", response["answer"])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
