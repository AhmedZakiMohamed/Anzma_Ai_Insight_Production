from __future__ import annotations

import unittest

from app.domain.identity import (AssetIdentity, BuildingIdentity, ExternalAssetMapping, IdentityConflict,
                                 IdentityMappings, IdentityNotFound, InvalidExternalId, SourceIdentity)


class InMemoryIdentityStore:
    """Tiny deterministic store double for identity policy; no database/schema setup is needed."""

    def __init__(self):
        self.buildings = {
            "BLD-A": BuildingIdentity(1, "BLD-A"),
            "BLD-B": BuildingIdentity(2, "BLD-B"),
        }
        self.sources = {
            "SRC-A": SourceIdentity(11, "SRC-A", 1, "BLD-A"),
            "SRC-B": SourceIdentity(12, "SRC-B", 2, "BLD-B"),
        }
        self.assets = {
            (1, "DOOR-1"): AssetIdentity(101, "DOOR-1", "Reader 1", "ACCESS_CONTROL", 1, "BLD-A"),
            (1, "AHU-1"): AssetIdentity(102, "AHU-1", "Air Handler", "HVAC", 1, "BLD-A"),
            (2, "DOOR-2"): AssetIdentity(201, "DOOR-2", "Reader 2", "ACCESS_CONTROL", 2, "BLD-B"),
        }
        self.mappings = {}

    def get_building(self, building_code):
        return self.buildings.get(building_code)

    def get_source(self, source_code):
        return self.sources.get(source_code)

    def get_asset_in_building(self, asset_code, building_id):
        return self.assets.get((building_id, asset_code))

    def get_external_mapping(self, source_id, external_id):
        return self.mappings.get((source_id, external_id))

    def insert_external_mapping(self, source, external_id, asset):
        key = (source.source_id, external_id)
        if key in self.mappings:
            return self.mappings[key], False
        mapping = ExternalAssetMapping(source.source_id, source.source_code, external_id,
                                       asset.asset_id, asset.asset_code, asset.asset_name, asset.system_code,
                                       asset.building_id, asset.building_code)
        self.mappings[key] = mapping
        return mapping, True


class IdentityMappingsTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryIdentityStore()
        self.identities = IdentityMappings(self.store)

    def test_resolves_building_and_source_to_their_existing_building(self):
        self.assertEqual(self.identities.building("BLD-A"), BuildingIdentity(1, "BLD-A"))
        self.assertEqual(self.identities.source("SRC-A"), SourceIdentity(11, "SRC-A", 1, "BLD-A"))

    def test_register_is_idempotent_and_external_id_stays_bound(self):
        first = self.identities.map_asset(source_code="SRC-A", external_id="vendor:door-1", asset_code="DOOR-1")
        retry = self.identities.map_asset(source_code="SRC-A", external_id="vendor:door-1", asset_code="DOOR-1")
        self.assertTrue(first.created)
        self.assertFalse(retry.created)
        self.assertEqual(first.mapping, retry.mapping)
        self.assertEqual(self.identities.resolve_asset(source_code="SRC-A", external_id="vendor:door-1"),
                         first.mapping)

    def test_conflicting_reassignment_is_rejected_without_changing_mapping(self):
        original = self.identities.map_asset(source_code="SRC-A", external_id="vendor:id-7",
                                             asset_code="DOOR-1").mapping
        with self.assertRaisesRegex(IdentityConflict, "already mapped to a different asset"):
            self.identities.map_asset(source_code="SRC-A", external_id="vendor:id-7", asset_code="AHU-1")
        self.assertEqual(self.identities.resolve_asset(source_code="SRC-A", external_id="vendor:id-7"), original)

    def test_external_ids_are_scoped_to_source_and_cannot_cross_buildings(self):
        a = self.identities.map_asset(source_code="SRC-A", external_id="device-1", asset_code="DOOR-1")
        b = self.identities.map_asset(source_code="SRC-B", external_id="device-1", asset_code="DOOR-2")
        self.assertNotEqual(a.mapping.source_id, b.mapping.source_id)
        with self.assertRaisesRegex(IdentityNotFound, "source's building"):
            self.identities.map_asset(source_code="SRC-A", external_id="wrong-site", asset_code="DOOR-2")

    def test_door_mapping_requires_access_control_asset(self):
        mapped = self.identities.map_door(source_code="SRC-A", external_id="controller:door-1",
                                          asset_code="DOOR-1")
        self.assertEqual(mapped.mapping.system_code, "ACCESS_CONTROL")
        with self.assertRaisesRegex(IdentityConflict, "requires a ACCESS_CONTROL asset"):
            self.identities.map_door(source_code="SRC-A", external_id="not-a-door", asset_code="AHU-1")
        self.assertEqual(self.identities.resolve_door(source_code="SRC-A", external_id="controller:door-1"),
                         mapped.mapping)
        self.assertIsNone(self.identities.resolve_door(source_code="SRC-A", external_id="unmapped"))

    def test_invalid_external_ids_are_rejected_without_normalizing_them(self):
        for value in ("", "   ", "x" * 129):
            with self.subTest(value_length=len(value)), self.assertRaises(InvalidExternalId):
                self.identities.map_asset(source_code="SRC-A", external_id=value, asset_code="DOOR-1")
        exact = " Vendor:Door/01 "
        result = self.identities.map_asset(source_code="SRC-A", external_id=exact, asset_code="DOOR-1")
        self.assertEqual(result.mapping.external_id, exact)

    def test_missing_source_and_building_are_reported(self):
        with self.assertRaises(IdentityNotFound):
            self.identities.source("NO-SUCH-SOURCE")
        with self.assertRaises(IdentityNotFound):
            self.identities.building("NO-SUCH-BUILDING")


if __name__ == "__main__":
    unittest.main()
