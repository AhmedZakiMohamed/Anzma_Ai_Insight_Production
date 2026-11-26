"""Focused tests for Task 7's deterministic Tool Envelope validation."""
import unittest

from app.domain.evidence.envelope_evidence import EvidenceCode, validate_envelope_evidence
from app.domain.evidence.numeric_integrity import (deterministic_fallback, result_fingerprint,
                                                   validate_numeric_claims, verify_fingerprint)
from app.domain.evidence.tool_answer_guard import guard_tool_answer


def result():
    return {
        "result_id": "res_demo", "tool": "doors.after_hours", "query_version": "1",
        "status": "COMPLETE", "parameters": {"building": "MAIN-TOWER", "start": "2026-11-01", "end": "2026-11-01"},
        "data_as_of": "2026-11-01T12:00:00Z", "generated_at": "2026-11-01T12:01:00Z",
        "source_ids": ["source-a"], "complete": True, "quality": "GOOD", "truncated": False,
        "columns": ["asset_code", "opening_count"],
        "rows": [{"asset_code": "DOOR-1", "opening_count": 2, "evidence_refs": []}],
        "total_rows": 1, "limitations": [], "trace_id": "trace-a",
    }


class EnvelopeEvidenceUnitTests(unittest.TestCase):
    def test_missing_building_scope_fails_before_database_lookup(self):
        out = validate_envelope_evidence(object(), {"parameters": {}, "rows": []})
        self.assertFalse(out.ok)
        self.assertEqual(out.blocking_issues[0].code, EvidenceCode.BUILDING_SCOPE_MISSING)

    def test_trusted_building_authorization_does_not_come_from_result(self):
        out = validate_envelope_evidence(object(), result(), authorized_building_codes={"OTHER"})
        self.assertFalse(out.ok)
        self.assertEqual(out.blocking_issues[0].code, EvidenceCode.BUILDING_NOT_AUTHORIZED)

    def test_noncanonical_reference_id_is_rejected_without_database_lookup(self):
        data = result()
        data["rows"][0]["evidence_refs"] = [{"kind": "event_record", "id": "01"}]
        out = validate_envelope_evidence(object(), data)
        self.assertFalse(out.ok)
        self.assertEqual(out.blocking_issues[0].code, EvidenceCode.MALFORMED_EVIDENCE_ID)


class NumericIntegrityUnitTests(unittest.TestCase):
    def test_fingerprint_ignores_clock_and_normalizes_reference_sets(self):
        first = result()
        first["rows"][0]["evidence_refs"] = [{"kind": "event_record", "id": "2"},
                                              {"kind": "event_record", "id": "1"}]
        second = result()
        second["generated_at"] = "2026-11-02T00:00:00Z"
        second["trace_id"] = "different"
        second["rows"][0]["evidence_refs"] = list(reversed(first["rows"][0]["evidence_refs"]))
        self.assertEqual(result_fingerprint(first), result_fingerprint(second))
        self.assertTrue(verify_fingerprint(first, result_fingerprint(first)))

    def test_fingerprint_changes_when_authoritative_metric_changes(self):
        first = result()
        second = result()
        second["rows"][0]["opening_count"] = 3
        self.assertNotEqual(result_fingerprint(first), result_fingerprint(second))

    def test_numeric_claims_accept_authoritative_count_and_reject_wrong_count(self):
        data = result()
        self.assertTrue(validate_numeric_claims("DOOR-1 had 2 openings.", data).ok)
        bad = validate_numeric_claims("DOOR-1 had 3 openings.", data)
        self.assertFalse(bad.ok)

    def test_fallback_is_deterministic_and_guard_uses_it_without_model_answer(self):
        data = result()
        first, second = deterministic_fallback(data), deterministic_fallback(data)
        self.assertEqual(first, second)
        self.assertTrue(validate_numeric_claims(first, data).ok)
        outcome = guard_tool_answer(answer=None, result=data, require_evidence=False)
        self.assertEqual(outcome.ai_source, "rules")
        self.assertEqual(outcome.answer, first)
        self.assertFalse(outcome.model_answer_accepted)


if __name__ == "__main__":
    unittest.main()
