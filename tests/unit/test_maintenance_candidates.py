from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain.tools import maintenance_candidates as mc
from app.domain.tools.envelope import Row

ROOT = Path(__file__).resolve().parents[2]


def test_manual_golden_normalization_examples():
    golden = json.loads((ROOT / "tests/fixtures/maintenance_candidates_v1.json").read_text())
    assert golden["policy_version"] == mc.POLICY_VERSION
    for item in golden["normalization_examples"]["faults"]:
        assert mc.normalize_count_faults(item["independent_faults"]) == item["score"]
    for item in golden["normalization_examples"]["condition"]:
        assert mc.normalize_condition(item["value"], item["warning"], item["critical"]) == item["score"]
    for item in golden["normalization_examples"]["downtime"]:
        assert mc.normalize_downtime(item["seconds"]) == item["score"]


def test_profile_weights_priority_and_single_fault_never_high():
    golden = json.loads((ROOT / "tests/fixtures/maintenance_candidates_v1.json").read_text())
    for item in golden["priority_examples"]:
        score, priority, profile, _, _ = mc.calculate_priority(
            fault_count=item["fault_count"], downtime_seconds=item["downtime_seconds"],
            condition_score=item["condition_score"], availability_eligible=item["profile"] == "availability-v1",
            coverage_complete=True, condition_supported=True)
        assert (profile, score, priority) == (item["profile"], item["total_score"], item["priority"])
    one_fault, priority, *_ = mc.calculate_priority(
        fault_count=1, downtime_seconds=None, condition_score=0, availability_eligible=False,
        coverage_complete=True, condition_supported=True)
    assert one_fault == 15.0
    assert priority == "LOW"


def test_policy_can_only_be_configured_through_server_owned_object_and_versions_result():
    custom = mc.ScoringPolicy(version="maintenance-priority-test-2", fault_condition_weights=(0.5, 0.5))
    score, _, _, _, _ = mc.calculate_priority(
        fault_count=2, downtime_seconds=None, condition_score=80, availability_eligible=False,
        coverage_complete=True, condition_supported=True, policy=custom)
    assert score == 65.0
    spec = mc.ToolSpec(mc.TOOL_NAME, mc.DESCRIPTION, mc.PARAMETERS_SCHEMA, lambda **_: None, mc.TOOL_VERSION)
    problems = spec.validate_arguments({"building": "MAIN", "start": "2026-01-01", "end": "2026-01-01",
                                        "scoring_policy": {"fault_condition_weights": [0.5, 0.5]}})
    assert "unknown argument: scoring_policy" in problems


@pytest.mark.parametrize("kwargs", [
    {"fault_count": 3, "downtime_seconds": None, "condition_score": 100,
     "availability_eligible": False, "coverage_complete": False, "condition_supported": True},
    {"fault_count": 3, "downtime_seconds": None, "condition_score": 100,
     "availability_eligible": False, "coverage_complete": True, "condition_supported": False},
    {"fault_count": 3, "downtime_seconds": None, "condition_score": 100,
     "availability_eligible": True, "coverage_complete": True, "condition_supported": True},
])
def test_missing_coverage_or_factor_withholds_score(kwargs):
    score, priority, _, _, _ = mc.calculate_priority(**kwargs)
    assert score is None
    assert priority == "INSUFFICIENT_DATA"


def test_coverage_requires_events_telemetry_and_full_window():
    start = mc.datetime(2026, 1, 1, tzinfo=mc.timezone.utc)
    end = mc.datetime(2026, 1, 2, tzinfo=mc.timezone.utc)
    as_of = end
    good = mc.MaintenanceSourceCoverage(True, True, True, start, end, as_of)
    events_only = mc.MaintenanceSourceCoverage(True, False, True, start, end, as_of)
    provider = lambda _b, _s, _e, codes: {code: good for code in codes}
    assert mc.assess_maintenance_coverage(provider, "BLD", ["SRC"], start, end).complete
    partial = mc.assess_maintenance_coverage(lambda *_: {"SRC": events_only}, "BLD", ["SRC"], start, end)
    assert not partial.complete
    no_provider = mc.assess_maintenance_coverage(None, "BLD", ["SRC"], start, end)
    assert not no_provider.complete


def test_sql_is_read_only_static_and_window_bounded():
    for sql in (mc.SQL_FAULTS, mc.SQL_CONDITION, mc.SQL_UNKNOWN_QUALITY, mc.SQL_UNSUPPORTED_UNITS, mc.SQL_SOURCES):
        assert "%(building_id)s" in sql or "%(building)s" in sql
        assert "DELETE " not in sql.upper()
        assert "UPDATE " not in sql.upper()
        assert "INSERT " not in sql.upper()
    assert "%(start_utc)s" in mc.SQL_FAULTS and "%(end_utc)s" in mc.SQL_FAULTS
    assert "%(start_utc)s" in mc.SQL_CONDITION and "%(end_utc)s" in mc.SQL_CONDITION
    assert "%(start_utc)s" in mc.SQL_SOURCES and "%(end_utc)s" in mc.SQL_SOURCES
    assert "LIMIT 20001" in mc.SQL_FAULTS
    for sql in (mc.SQL_CONDITION, mc.SQL_UNKNOWN_QUALITY, mc.SQL_UNSUPPORTED_UNITS):
        assert "LIMIT 2001" in sql
    assert "alerts" not in mc.SQL_FAULTS.lower() and "incidents" not in mc.SQL_FAULTS.lower()
    assert mc.MAX_WINDOW_DAYS == 90


def test_condition_mapping_uses_canonical_units_and_only_supported_systems():
    assert mc.CONDITION_METRICS["HVAC"]["temperature"][0] == "C"
    assert mc.CONDITION_METRICS["NETWORKING"]["packet_loss"][0] == "%"
    assert "CCTV" not in mc.CONDITION_METRICS
    assert "FIRE_ALARM" not in mc.CONDITION_METRICS


def test_tool_spec_does_not_expose_policy_or_coverage_as_model_arguments():
    spec = mc.ToolSpec(mc.TOOL_NAME, mc.DESCRIPTION, mc.PARAMETERS_SCHEMA, lambda **_: None, mc.TOOL_VERSION)
    assert spec.validate_arguments({"building": "MAIN", "start": "2026-01-01", "end": "2026-01-01"}) == []
    assert spec.validate_arguments({"building": "MAIN", "start": "2026-01-01", "end": "2026-01-01",
                                    "weights": {"fault": 100}})


def test_unobserved_condition_is_not_normalized_to_healthy_zero():
    score, priority, _, _, _ = mc.calculate_priority(
        fault_count=2, downtime_seconds=None, condition_score=None, availability_eligible=False,
        coverage_complete=True, condition_supported=True)
    assert score is None
    assert priority == "INSUFFICIENT_DATA"


def test_fault_recurrence_is_withheld_when_source_identity_is_missing():
    score, priority, *_ = mc.calculate_priority(
        fault_count=4, downtime_seconds=None, condition_score=100, availability_eligible=False,
        coverage_complete=True, condition_supported=True, fault_identity_complete=False)
    assert score is None
    assert priority == "INSUFFICIENT_DATA"


def test_tied_rows_have_stable_order_and_profile_local_ranks():
    def row(code):
        return Row({"scoring_profile": "fault-condition-v1", "score": 50.0,
                    "building_code": "BLD", "system_type": "HVAC", "asset_code": code})
    ranked = mc.rank_within_profiles([row("Z"), row("A")])
    assert [item.values["asset_code"] for item in ranked] == ["A", "Z"]
    assert [item.values["rank_within_profile"] for item in ranked] == [1, 2]
