from __future__ import annotations

import json
import unittest
from pathlib import Path

from connectors.sdk import CanonicalEvent, ConnectorManifest, Page, load_manifest


class ConnectorSDKContractTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parents[2] / "connectors" / "reference_rest" / "manifest.json"
        self.manifest = json.loads(path.read_text(encoding="utf-8"))

    def test_reference_manifest_matches_common_contract(self):
        path = Path(__file__).parents[2] / "connectors" / "reference_rest" / "manifest.json"
        manifest = load_manifest(path)
        self.assertEqual(manifest.id, "anzma.reference-rest")
        self.assertEqual(manifest.capabilities, ("history.events",))

    def test_manifest_rejects_duplicate_capabilities(self):
        malformed = {**self.manifest, "capabilities": ["history.events", "history.events"]}
        with self.assertRaisesRegex(ValueError, "duplicates"):
            ConnectorManifest.from_mapping(malformed)

    def test_manifest_rejects_unknown_fields_instead_of_silently_ignoring_them(self):
        malformed = {**self.manifest, "capabilties": ["history.events"]}
        with self.assertRaisesRegex(ValueError, "fields mismatch"):
            ConnectorManifest.from_mapping(malformed)

    def test_page_preserves_partial_coverage(self):
        page = Page(items=("evt-1",), next_cursor="cursor-2", complete=False)
        self.assertFalse(page.complete)
        self.assertEqual(page.next_cursor, "cursor-2")

    def test_canonical_event_requires_utc_observation_time(self):
        with self.assertRaisesRegex(ValueError, "UTC"):
            CanonicalEvent("evt-1", "door-1", "door_opened", "2026-11-25T20:00:00+02:00")
        event = CanonicalEvent("evt-1", "door-1", "door_opened", "2026-11-25T18:00:00Z")
        self.assertEqual(event.event_type, "door_opened")


if __name__ == "__main__":
    unittest.main()
