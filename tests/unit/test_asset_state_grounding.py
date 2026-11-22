from __future__ import annotations

import unittest

from app.domain.ai.explanation import explain
from app.domain.ai.model_provider import ModelReply
from app.domain.evidence import validation


class FakeProvider:
    def __init__(self, seconds: int):
        self.seconds = seconds

    def complete(self, **_kwargs):
        return ModelReply(
            content=(
                '{"summary":"The cited series was STALE for '
                f'{self.seconds} seconds at 12:00 [E1]."'
                ',"cause_ids":[],"action_ids":[],"evidence_refs":["E1"],"confidence":"medium"}'
            )
        )


def fixture_alert():
    return {
        "rule_id": "HVAC_HIGH_TEMP", "severity": "WARNING", "detection_method": "THRESHOLD",
        "asset_id": 1, "summary": "Temperature alert", "first_ts": "2026-11-22T12:00:00+00:00",
        "last_ts": "2026-11-22T12:00:00+00:00",
        "evidence": {"thresholds": {}, "facts": {}, "event_count": 1, "events": [
            {"ref": "E1", "source": "telemetry", "record_id": 42, "telemetry_id": 42,
             "ts": "2026-11-22T12:00:00+00:00", "event_type": "TEMPERATURE_READING",
             "value": 33.5, "unit": "C", "details": {}}
        ]},
        "_g3_evidence": {
            "schema_version": 1,
            "series_freshness": [{"ref": "E1", "state": "STALE", "age_seconds": 600,
                                  "evaluated_at_utc": "2026-11-22 12:00"}],
            "cited_rows": [{"ref": "E1", "series_freshness": {"state": "STALE", "age_seconds": 600,
                                                                   "expected_interval_seconds": 300,
                                                                   "evaluated_at_utc": "2026-11-22 12:00"}}],
            "uncertain_evidence_refs": ["E1"],
        },
    }


class AssetStateGroundingTests(unittest.TestCase):
    def test_supported_freshness_field_does_not_force_fallback(self):
        result = explain(fixture_alert(), {"name": "AHU-1", "system_type": "HVAC", "zone": "Plant"},
                         provider=FakeProvider(600))
        self.assertEqual(result["source"], "LLM")
        self.assertTrue(result["grounding"]["passed"])
        self.assertEqual(result["grounding"]["violations"], [])

    def test_unsupported_claim_remains_rejected_with_g3_fields_present(self):
        result = explain(fixture_alert(), {"name": "AHU-1", "system_type": "HVAC", "zone": "Plant"},
                         provider=FakeProvider(900))
        self.assertEqual(result["source"], "FALLBACK")
        rejected = result["grounding"]["rejected_answers"]
        self.assertTrue(any("number 900 not in evidence" in " ".join(item["violations"])
                            for item in rejected))

    def test_uncertain_quality_only_lowers_sufficiency_when_final_text_cites_it(self):
        alert = fixture_alert()
        sufficiency, limitation = validation.assess_cited_uncertainty(alert, "Reading 33.5 [E1].")
        self.assertEqual(sufficiency, "partial")
        self.assertIn("UNCERTAIN", limitation)
        self.assertEqual(validation.assess_cited_uncertainty(alert, "Reading 33.5 [E2]."), (None, None))
        self.assertEqual(validation.assess_cited_uncertainty(alert, "Reading is uncertain without a citation."),
                         (None, None))


if __name__ == "__main__":
    unittest.main()
