from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.domain.asset_state import derive_asset_state


class AssetStateDerivationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)

    def test_dashboard_buckets_map_without_changing_legacy_semantics(self):
        expected = {"critical": "CRITICAL", "warning": "WARNING", "normal": "NORMAL",
                    "unknown": "UNKNOWN", "no_data": "UNKNOWN"}
        for bucket, state in expected.items():
            with self.subTest(bucket=bucket):
                self.assertEqual(derive_asset_state(bucket, [], None, self.now)["operational_state"], state)

    def test_single_source_communication_uses_series_freshness_and_injected_time(self):
        for freshness, communication in (("FRESH", "COMMUNICATING"), ("STALE", "DEGRADED"),
                                         ("MISSING", "SILENT")):
            with self.subTest(freshness=freshness):
                result = derive_asset_state("normal", [{"source_id": 4, "freshness": freshness}],
                                            "UNAVAILABLE", self.now)
                self.assertEqual(result["communication_status"], communication)
                self.assertEqual(result["availability"], "UNAVAILABLE")
                self.assertEqual(result["evaluated_at"], self.now)
                self.assertNotIn(result["communication_status"], {"OFFLINE", "UNAVAILABLE"})

    def test_multisource_and_mixed_legacy_attribution_are_unknown(self):
        two_sources = derive_asset_state("normal", [{"source_id": 4, "freshness": "FRESH"}],
                                         None, self.now, all_source_ids=[4, 5])
        legacy_mixed = derive_asset_state("normal", [{"source_id": None, "freshness": "FRESH"}],
                                          None, self.now, all_source_ids=[4])
        self.assertEqual(two_sources["communication_status"], "UNKNOWN")
        self.assertEqual(two_sources["source_ids"], [4, 5])
        self.assertTrue(two_sources["attribution_ambiguous"])
        self.assertEqual(legacy_mixed["communication_status"], "UNKNOWN")

    def test_connector_reported_availability_is_not_an_input(self):
        result = derive_asset_state("normal", [{"source_id": 8, "freshness": "FRESH",
                                                  "availability": "UNAVAILABLE"}],
                                    None, self.now)
        self.assertEqual(result["availability"], "UNKNOWN")
        self.assertEqual(result["communication_status"], "COMMUNICATING")


if __name__ == "__main__":
    unittest.main()
