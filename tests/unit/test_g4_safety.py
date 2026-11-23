"""G4 safety checks that need no database.

1. A database that has not applied migration 012 (no asset_state_current table) must not break alert
   explanations: alert_evidence_context has to degrade to "no context" after ONE catalogue lookup.
2. The private G3 evidence object (_g3_evidence) must never appear in a public explanation response.
"""
from __future__ import annotations

import json
import unittest

from app.domain.evidence import validation
from app.services.asset_state import alert_evidence_context


class RecordingCursor:
    """Minimal cursor double: records statements, answers the to_regclass lookup."""

    def __init__(self, table_name):
        self.table_name = table_name
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(" ".join(str(sql).split()))

    def fetchone(self):
        return {"table_name": self.table_name}

    def fetchall(self):  # pragma: no cover - must not be reached when the table is absent
        raise AssertionError("no further query may run when asset_state_current is absent")


def alert():
    return {
        "rule_id": "HVAC_HIGH_TEMP", "severity": "WARNING", "detection_method": "THRESHOLD",
        "asset_id": 1, "summary": "Temperature alert",
        "first_ts": "2026-11-22T12:00:00+00:00", "last_ts": "2026-11-22T12:00:00+00:00",
        "evidence": {"thresholds": {}, "facts": {}, "event_count": 0, "events": []},
    }


class DatabaseWithoutMigration012Tests(unittest.TestCase):
    def test_missing_state_table_returns_no_context_without_further_queries(self):
        cur = RecordingCursor(table_name=None)
        self.assertIsNone(alert_evidence_context(cur, alert()))
        self.assertEqual(len(cur.statements), 1)
        self.assertIn("to_regclass('public.asset_state_current')", cur.statements[0])

    def test_explanation_still_works_when_context_is_absent(self):
        data = alert()
        data["_g3_evidence"] = None  # what pipeline.explain_alert stores for a pre-012 database
        response = validation.explain_alert(data, {"name": "AHU-1", "system_type": "HVAC", "zone": "Plant"},
                                            use_llm=False)
        self.assertIn("answer", response)
        self.assertIn("validation", response)


class PrivateEvidenceStaysPrivateTests(unittest.TestCase):
    def test_g3_evidence_is_not_in_the_public_response(self):
        data = alert()
        data["_g3_evidence"] = {"schema_version": 1, "series_freshness": [], "cited_rows": [],
                                "uncertain_evidence_refs": []}
        response = validation.explain_alert(data, {"name": "AHU-1", "system_type": "HVAC", "zone": "Plant"},
                                            use_llm=False)
        self.assertNotIn("_g3_evidence", response)
        self.assertNotIn("_g3_evidence", json.dumps(response, default=str))


if __name__ == "__main__":
    unittest.main()
