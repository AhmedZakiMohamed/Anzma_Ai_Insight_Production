"""Parity between the migration-014 catalogue (tests/fixtures/event_catalog_014.json) and the legacy pair lists.

No database. The legacy lists are intentionally NOT changed by migration 014; this test fails the moment
they drift from the catalogue the migration writes (ADR-0001 follow-up 2, CORE_DATA_ARCHITECTURE F-08).
The same JSON is compared with the real database in tests/integration/test_event_catalogue_migration.py.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

from app.adapters.database import db_adapter
from app.api import chat_facts
from app.domain.operations import integrity_rules
from app.repositories import dashboard_core_pg as core

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = json.loads((ROOT / "tests/fixtures/event_catalog_014.json").read_text(encoding="utf-8"))
CATALOGUE = EXPECTED["catalogue"]


def expected_pairs(outage_class=None):
    """{(opener, closer)} derived from the catalogue."""
    return {(row["resolves"], name) for name, row in CATALOGUE.items()
            if row["resolves"] and (outage_class is None or row["outage_class"] == outage_class)}


class CatalogueShapeTests(unittest.TestCase):
    def test_every_seed_type_is_classified_and_new_types_are_added(self):
        self.assertEqual(set(CATALOGUE), set(EXPECTED["seed_event_types"]) | set(EXPECTED["new_event_types"]))
        self.assertEqual(len(EXPECTED["seed_event_types"]), 12)

    def test_fixture_obeys_the_constraints_migration_014_adds(self):
        for name, row in CATALOGUE.items():
            is_pair_class = row["event_class"] in ("FAULT_OPEN", "FAULT_CLOSE")
            self.assertEqual(is_pair_class, row["outage_class"] is not None, name)
            self.assertEqual(row["event_class"] == "FAULT_CLOSE", row["resolves"] is not None, name)
            if row["resolves"]:
                opener = CATALOGUE[row["resolves"]]
                self.assertEqual(opener["event_class"], "FAULT_OPEN", name)
                self.assertEqual(opener["outage_class"], row["outage_class"], name)
        resolved = [row["resolves"] for row in CATALOGUE.values() if row["resolves"]]
        self.assertEqual(len(resolved), len(set(resolved)), "one closer per opener")


class LegacyListParityTests(unittest.TestCase):
    def test_dashboard_fault_recovery_pairs_match(self):
        self.assertEqual(set(core.FAULT_RECOVERY_PAIRS), expected_pairs())

    def test_chat_facts_pairs_match(self):
        self.assertEqual(set(chat_facts.PAIRS), expected_pairs())

    def test_chat_facts_sql_hardcodes_the_same_openers_and_closers(self):
        source = (ROOT / "app/api/chat_facts.py").read_text(encoding="utf-8")
        start = source.index("def _faults(")
        body = source[start:source.index("\ndef ", start + 1)]
        in_list = re.search(r"et\.name IN \(([^)]*)\) THEN", body)
        case_block = re.search(r"t2\.name = CASE et\.name(.*?)END\)", body, re.S)
        self.assertTrue(in_list and case_block, "chat_facts._faults SQL changed; update this parity test")
        openers = {o for o, _ in expected_pairs()}
        self.assertEqual(set(re.findall(r"'([a-z_]+)'", in_list.group(1))), openers)
        when_pairs = set(re.findall(r"WHEN '([a-z_]+)' THEN '([a-z_]+)'", case_block.group(1)))
        else_closer = re.search(r"ELSE '([a-z_]+)'", case_block.group(1))
        self.assertIsNotNone(else_closer)
        # the ELSE branch is the one opener without a WHEN
        else_opener = openers - {o for o, _ in when_pairs}
        self.assertEqual(len(else_opener), 1)
        self.assertEqual(when_pairs | {(next(iter(else_opener)), else_closer.group(1))}, expected_pairs())

    def test_integrity_rule_closers_match_after_name_translation(self):
        """CLOSERS uses engine names and omits hvac (the engine has no HVAC pair); translate through EVENT_MAP."""
        to_engine = {name: engine for name, (engine, _unit) in db_adapter.EVENT_MAP.items()}
        translated = {(to_engine[o], to_engine[c]) for o, c in expected_pairs() if o in to_engine and c in to_engine}
        self.assertEqual(translated, set(integrity_rules.CLOSERS.items()))
        untranslatable = {(o, c) for o, c in expected_pairs() if o not in to_engine or c not in to_engine}
        self.assertEqual(untranslatable, {("hvac_fault", "hvac_recovered")})

    def test_event_map_only_names_catalogue_types(self):
        self.assertLessEqual(set(db_adapter.EVENT_MAP), set(CATALOGUE))

    def test_availability_pairs_are_exactly_the_cctv_and_network_pairs(self):
        self.assertEqual(expected_pairs("AVAILABILITY"),
                         {("link_down", "link_up"), ("camera_offline", "camera_online")})
        self.assertEqual(expected_pairs("FAULT"),
                         {("hvac_fault", "hvac_recovered"), ("fire_alarm_triggered", "alarm_cleared")})


if __name__ == "__main__":
    unittest.main()
