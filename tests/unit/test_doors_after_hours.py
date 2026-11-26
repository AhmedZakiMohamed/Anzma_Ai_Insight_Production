"""doors.after_hours: period/DST arithmetic, counting rules, completeness and the golden-fixture comparison.

No database: a fake cursor replays the repo's SQL against in-memory rows. The SQL itself is exercised by
tests/integration/test_doors_after_hours_sql.py on a disposable database.
"""
import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.domain.tools import doors_after_hours as dah
from app.domain.tools.coverage import SourceCoverage
from app.domain.tools.envelope import ResultStatus
from app.domain.tools.periods import PeriodError, build_after_hours_period
from app.domain.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc


def u(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


class FakeCursor:
    """Answers the three SQL statements of the tool from in-memory data; records every execute call."""

    def __init__(self, building, events, sources=("SRC-A",)):
        self.building, self.events, self.sources = building, events, list(sources)
        self.calls, self._result = [], None

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql == dah.SQL_BUILDING:
            self._result = [self.building] if params["building_code"] == self.building["building_code"] else []
        elif sql == dah.SQL_DOOR_SOURCES:
            self._result = [{"source_code": s} for s in self.sources]
        elif sql == dah.SQL_EVENTS:
            wins = list(zip(params["window_starts"], params["window_ends"]))
            self._result = [e for e in self.events if e["event_type"] in (params["opening_event"], params["forced_event"])
                            and any(a <= e["ts"] < b for a, b in wins)]
        else:
            raise AssertionError("unexpected SQL")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


def ev(i, ts, etype="door_opened", asset=1, ident="auto", source_id=1):
    return {"event_id": i, "ts": u(ts), "event_type": etype, "source_id": source_id, "source_code": "SRC-A",
            "source_event_id": f"E{i}" if ident == "auto" else ident, "asset_id": asset, "asset_code": f"D{asset}",
            "asset_name": f"Door {asset}", "external_ids": [f"EXT-{asset}"]}


CAIRO = {"building_id": 1, "building_code": "BLD-A", "timezone": "Africa/Cairo"}
COVERED = lambda b, s, e, codes: {c: SourceCoverage(c, True, True, s, e, e) for c in codes}


def run(cur, provider=None, confirmed=frozenset(), **kw):
    args = dict(building="BLD-A", start="2026-11-01", end="2026-11-01")
    args.update(kw)
    return dah.run_doors_after_hours(cur, coverage_provider=provider, confirmed_timezone_buildings=confirmed, **args)


class PeriodTests(unittest.TestCase):
    def test_cairo_after_hours_window_boundaries(self):  # fixture S-A: [20:00, next local midnight)
        p = build_after_hours_period("2026-11-01", "2026-11-01", "20:00", "Africa/Cairo")
        self.assertEqual(p.windows, ((u("2026-11-01T18:00:00Z"), u("2026-11-01T22:00:00Z")),))
        self.assertEqual((p.start_utc, p.end_utc_exclusive), (u("2026-10-31T22:00:00Z"), u("2026-11-01T22:00:00Z")))

    def test_dst_fall_back_day_is_25_hours_long(self):  # fixture S-B, Europe/Berlin
        p = build_after_hours_period("2026-10-24", "2026-10-26", None, "Europe/Berlin")
        self.assertEqual(p.start_utc, u("2026-10-23T22:00:00Z"))
        self.assertEqual(p.end_utc_exclusive, u("2026-10-26T23:00:00Z"))
        self.assertEqual(p.windows[1], (u("2026-10-25T19:00:00Z"), u("2026-10-25T23:00:00Z")))  # 20:00 CET (+01:00) .. midnight CET
        self.assertEqual(p.windows[0], (u("2026-10-24T18:00:00Z"), u("2026-10-24T22:00:00Z")))  # still CEST

    def test_nonexistent_midnight_uses_first_valid_instant(self):  # D-10; Cairo skips 00:00 on 2026-04-24
        p = build_after_hours_period("2026-04-24", "2026-04-24", "20:00", "Africa/Cairo")
        self.assertEqual(p.start_utc, u("2026-04-23T22:00:00Z"))  # 01:00 local, the first valid instant of that day

    def test_repeated_midnight_uses_first_occurrence(self):  # D-10; Havana repeats 00:00-01:00 on 2026-11-01
        p = build_after_hours_period("2026-11-01", "2026-11-01", "20:00", "America/Havana")
        self.assertEqual(p.start_utc, u("2026-11-01T04:00:00Z"))

    def test_ambiguous_after_local_is_an_error_not_a_guess(self):
        with self.assertRaises(PeriodError) as cm:
            build_after_hours_period("2026-10-25", "2026-10-25", "02:30", "Europe/Berlin")
        self.assertEqual(cm.exception.code, "INVALID_PARAMETER")

    def test_timestamp_form_requires_offset_and_clips_windows(self):  # G-TZ-02
        with self.assertRaises(PeriodError):
            build_after_hours_period("2026-11-01T00:00:00", "2026-11-02T00:00:00", None, "Africa/Cairo")
        p = build_after_hours_period("2026-11-01T19:00:00+00:00", "2026-11-01T21:00:00Z", "20:00", "Africa/Cairo")
        self.assertEqual(p.windows, ((u("2026-11-01T19:00:00Z"), u("2026-11-01T21:00:00Z")),))

    def test_bad_inputs(self):
        cases = [("2026-11-02", "2026-11-01", "Africa/Cairo"), ("2026-11-01", "2026-11-01T00:00:00Z", "Africa/Cairo"),
                 ("2026-02-30", "2026-03-01", "Africa/Cairo"), ("2026-01-01", "2027-06-01", "Africa/Cairo")]
        for s, e, tz in cases:
            with self.assertRaises(PeriodError, msg=(s, e)):
                build_after_hours_period(s, e, "20:00", tz)
        for tz in (None, "", "Mars/Olympus"):
            with self.assertRaises(PeriodError) as cm:
                build_after_hours_period("2026-11-01", "2026-11-01", "20:00", tz)
            self.assertEqual(cm.exception.code, "TIMEZONE_UNAVAILABLE")
        with self.assertRaises(PeriodError):
            build_after_hours_period("2026-11-01", "2026-11-01", "25:00", "Africa/Cairo")


class CountingTests(unittest.TestCase):
    def codes(self, d):
        return [x["code"] for x in d["limitations"]]

    def test_forced_only_gives_no_rows_but_partial_with_disclosure(self):
        cur = FakeCursor(CAIRO, [ev(1, "2026-11-01T20:00:01Z", "door_forced")])
        d = run(cur, COVERED, confirmed={"BLD-A"}).to_dict()   # coverage and timezone are otherwise fully trusted
        self.assertEqual((d["status"], d["complete"], d["rows"], d["total_rows"]), ("PARTIAL", False, [], 0))
        self.assertEqual(self.codes(d), ["CORRELATION_UNAVAILABLE"])
        self.assertEqual(d["limitations"][0]["details"], {"uncounted_door_forced_events": 1})

    def test_opened_with_forced_counts_only_opened_and_is_partial(self):
        cur = FakeCursor(CAIRO, [ev(1, "2026-11-01T20:00:00Z"), ev(2, "2026-11-01T20:00:01Z", "door_forced"),
                                 ev(3, "2026-11-01T20:05:00Z", "door_forced")])
        d = run(cur, COVERED, confirmed={"BLD-A"}).to_dict()
        self.assertEqual((d["status"], d["complete"]), ("PARTIAL", False))
        self.assertEqual(d["rows"][0]["opening_count"], 1)                      # forced events never add to the count
        self.assertEqual(d["rows"][0]["uncounted_door_forced_events"], 2)       # per-door evidence preserved
        self.assertEqual([x["id"] for x in d["rows"][0]["evidence_refs"]], ["1"])
        self.assertEqual(self.codes(d), ["CORRELATION_UNAVAILABLE"])
        self.assertEqual(d["limitations"][0]["details"], {"uncounted_door_forced_events": 2})

    def test_no_forced_events_stays_complete_with_trusted_coverage(self):
        d = run(FakeCursor(CAIRO, [ev(1, "2026-11-01T20:00:00Z")]), COVERED, confirmed={"BLD-A"}).to_dict()
        self.assertEqual((d["status"], d["complete"]), ("COMPLETE", True))
        self.assertEqual(d["limitations"], [])
        self.assertEqual(d["rows"][0]["uncounted_door_forced_events"], 0)
        self.assertNotIn("CORRELATION_UNAVAILABLE", self.codes(d))

    def test_forced_outside_the_windows_does_not_downgrade(self):
        events = [ev(1, "2026-11-01T20:00:00Z"), ev(2, "2026-11-01T12:00:00Z", "door_forced"),   # working hours
                  ev(3, "2026-11-01T22:00:00Z", "door_forced")]                                   # exclusive window end
        d = run(FakeCursor(CAIRO, events), COVERED, confirmed={"BLD-A"}).to_dict()
        self.assertEqual(d["status"], "COMPLETE")

    def test_forced_without_trusted_coverage_reports_both_limitations(self):
        d = run(FakeCursor(CAIRO, [ev(1, "2026-11-01T20:00:00Z"), ev(2, "2026-11-01T20:01:00Z", "door_forced")])).to_dict()
        self.assertEqual(d["status"], "PARTIAL")
        self.assertEqual(sorted(self.codes(d)), ["COMPLETION_NOT_CONFIRMED", "CORRELATION_UNAVAILABLE", "DATA_AS_OF_UNKNOWN"])

    def test_period_with_no_after_hours_window_runs_cleanly(self):
        cur = FakeCursor(CAIRO, [ev(1, "2026-11-01T07:00:00Z")])
        d = run(cur, COVERED, confirmed={"BLD-A"}, start="2026-11-01T08:00:00+02:00", end="2026-11-01T10:00:00+02:00").to_dict()
        self.assertEqual((d["status"], d["total_rows"], d["parameters"]["window_count"]), ("COMPLETE", 0, 0))
        self.assertEqual(cur.calls[-1][1]["window_starts"], [])

    def test_forced_never_turns_unsupported_into_partial(self):
        nocap = lambda b, s, e, codes: {c: SourceCoverage(c, False, False, None, None, None) for c in codes}
        d = run(FakeCursor(CAIRO, [ev(1, "2026-11-01T20:00:01Z", "door_forced")]), nocap, confirmed={"BLD-A"}).to_dict()
        self.assertEqual((d["status"], d["rows"]), ("UNSUPPORTED", []))

    def test_window_boundaries_are_half_open(self):
        events = [ev(1, "2026-11-01T17:59:59Z"), ev(2, "2026-11-01T18:00:00Z"), ev(3, "2026-11-01T21:59:59Z"),
                  ev(4, "2026-11-01T22:00:00Z")]
        d = run(FakeCursor(CAIRO, events), COVERED, confirmed={"BLD-A"}).to_dict()
        self.assertEqual(d["rows"][0]["opening_count"], 2)
        self.assertEqual([x["id"] for x in d["rows"][0]["evidence_refs"]], ["2", "3"])

    def test_duplicate_delivery_collapses_and_missing_identity_is_flagged(self):  # G-DUP-01 / G-DUP-02
        events = [ev(1, "2026-11-01T20:00:00Z", ident="X"), ev(2, "2026-11-01T20:00:00Z", ident="X"),
                  ev(3, "2026-11-01T20:10:00Z", ident=None), ev(4, "2026-11-01T20:20:00Z", ident=None, asset=2)]
        d = run(FakeCursor(CAIRO, events), COVERED, confirmed={"BLD-A"}).to_dict()
        counts = {r["asset_code"]: r["opening_count"] for r in d["rows"]}
        self.assertEqual(counts, {"D1": 2, "D2": 1})
        lim = {x["code"]: x for x in d["limitations"]}
        self.assertEqual(lim["MISSING_EVENT_IDENTITY"]["details"]["openings_without_identity"], 2)

    def test_rows_only_for_doors_with_openings_and_ordering_is_stable(self):
        events = [ev(1, "2026-11-01T20:00:00Z", asset=3), ev(2, "2026-11-01T20:01:00Z", asset=2),
                  ev(3, "2026-11-01T20:02:00Z", asset=2), ev(4, "2026-11-01T20:03:00Z", "door_forced", asset=9)]
        d = run(FakeCursor(CAIRO, events), COVERED, confirmed={"BLD-A"}).to_dict()
        self.assertEqual([(r["asset_code"], r["opening_count"]) for r in d["rows"]], [("D2", 2), ("D3", 1)])

    def test_complete_zero_result_needs_confirmed_coverage(self):  # G-COMP-03 vs unconfirmed
        none_cur = lambda: FakeCursor(CAIRO, [])
        confirmed = run(none_cur(), COVERED, confirmed={"BLD-A"}).to_dict()
        self.assertEqual((confirmed["status"], confirmed["complete"], confirmed["total_rows"]), ("COMPLETE", True, 0))
        unconfirmed = run(none_cur(), None, confirmed={"BLD-A"}).to_dict()
        self.assertEqual((unconfirmed["status"], unconfirmed["complete"]), ("PARTIAL", False))


class CompletenessTests(unittest.TestCase):
    def test_default_is_partial_with_completion_not_confirmed(self):
        d = run(FakeCursor(CAIRO, [ev(1, "2026-11-01T20:00:00Z")])).to_dict()
        self.assertEqual((d["status"], d["complete"]), ("PARTIAL", False))
        self.assertIn("COMPLETION_NOT_CONFIRMED", [x["code"] for x in d["limitations"]])
        self.assertEqual(d["rows"][0]["opening_count"], 1)  # data still returned, just not claimed complete
        self.assertIsNone(d["data_as_of"])
        self.assertIn("DATA_AS_OF_UNKNOWN", [x["code"] for x in d["limitations"]])

    def test_coverage_must_span_the_whole_period(self):
        short = lambda b, s, e, codes: {c: SourceCoverage(c, True, True, s, e - timedelta(hours=1), e) for c in codes}
        d = run(FakeCursor(CAIRO, []), short, confirmed={"BLD-A"}).to_dict()
        self.assertEqual(d["status"], "PARTIAL")
        not_confirmed = lambda b, s, e, codes: {c: SourceCoverage(c, True, False, s, e, e) for c in codes}
        self.assertEqual(run(FakeCursor(CAIRO, []), not_confirmed, confirmed={"BLD-A"}).status, ResultStatus.PARTIAL)

    def test_confirmed_coverage_supplies_data_as_of(self):
        d = run(FakeCursor(CAIRO, []), COVERED, confirmed={"BLD-A"}).to_dict()
        self.assertEqual(d["data_as_of"], "2026-11-01T22:00:00Z")
        self.assertEqual(d["source_ids"], ["SRC-A"])

    def test_missing_capability_is_unsupported_not_zero(self):  # G-COMP-02
        nocap = lambda b, s, e, codes: {c: SourceCoverage(c, False, False, None, None, None) for c in codes}
        d = run(FakeCursor(CAIRO, [ev(1, "2026-11-01T20:00:00Z")]), nocap, confirmed={"BLD-A"}).to_dict()
        self.assertEqual((d["status"], d["rows"], d["complete"]), ("UNSUPPORTED", [], False))
        self.assertEqual(d["limitations"][0]["code"], "CAPABILITY_UNSUPPORTED")

    def test_backfill_timezone_is_never_complete_unless_operator_confirmed(self):
        gmt2 = dict(CAIRO, timezone="Etc/GMT-2")
        d = run(FakeCursor(gmt2, []), COVERED).to_dict()
        self.assertEqual((d["status"], d["complete"]), ("PARTIAL", False))
        self.assertIn("TIMEZONE_PROVENANCE_UNCONFIRMED", [x["code"] for x in d["limitations"]])
        ok = run(FakeCursor(gmt2, []), COVERED, confirmed=frozenset({"BLD-A"})).to_dict()
        self.assertEqual(ok["status"], "COMPLETE")
        explicit = run(FakeCursor(CAIRO, []), COVERED).to_dict()  # a non-backfill value cannot come from migration 009
        self.assertEqual(explicit["status"], "COMPLETE")

    def test_timezone_missing_or_invalid_is_cannot_compute(self):  # G-TZ-01
        for tz in (None, "", "Nope/Zone"):
            d = run(FakeCursor(dict(CAIRO, timezone=tz), [ev(1, "2026-11-01T20:00:00Z")]), COVERED).to_dict()
            self.assertEqual((d["status"], d["rows"]), ("CANNOT_COMPUTE", []))
            self.assertEqual(d["limitations"][0]["code"], "TIMEZONE_UNAVAILABLE")

    def test_unknown_building_and_bad_dates_are_cannot_compute(self):
        self.assertEqual(run(FakeCursor(CAIRO, []), building="BLD-X").status, ResultStatus.CANNOT_COMPUTE)
        self.assertEqual(run(FakeCursor(CAIRO, []), start="2026-11-05").status, ResultStatus.CANNOT_COMPUTE)


class ToolWiringTests(unittest.TestCase):
    def make_registry(self, cur):
        @contextmanager
        def factory():
            yield cur
        return ToolRegistry([dah.build_doors_after_hours_spec(factory)])

    def test_registered_tool_runs_through_registry_and_model_name(self):
        reg = self.make_registry(FakeCursor(CAIRO, [ev(1, "2026-11-01T20:00:00Z")]))
        self.assertEqual(reg.agent_tools()[0]["name"], "doors__after_hours")
        d = reg.call("doors__after_hours", {"building": "BLD-A", "start": "2026-11-01", "end": "2026-11-01"}).to_dict()
        self.assertEqual((d["tool"], d["query_version"], d["rows"][0]["opening_count"]), ("doors.after_hours", "1", 1))

    def test_caller_cannot_claim_completeness_or_inject_hooks(self):
        reg = self.make_registry(FakeCursor(CAIRO, []))
        for extra in ({"complete": True}, {"coverage_provider": "x"}, {"confirmed_timezone_buildings": ["BLD-A"]}):
            env = reg.call("doors.after_hours", {"building": "BLD-A", "start": "2026-11-01", "end": "2026-11-01", **extra})
            self.assertEqual(env.status, ResultStatus.CANNOT_COMPUTE, extra)

    def test_sql_is_static_and_fully_parameterised(self):
        cur = FakeCursor(CAIRO, [ev(1, "2026-11-01T20:00:00Z")])
        evil = "BLD-A'; DROP TABLE events;--"
        run(cur, building=evil)
        run(cur, building="BLD-A")
        for sql, params in cur.calls:
            self.assertNotIn("DROP", sql)
            self.assertNotIn("BLD-A", sql)
            self.assertIsInstance(params, dict)
        self.assertEqual(len(cur.calls), 1 + 3)
        for sql in (dah.SQL_BUILDING, dah.SQL_DOOR_SOURCES, dah.SQL_EVENTS):
            self.assertTrue(sql.lstrip().upper().startswith(("SELECT", "WITH")))


class GoldenFixtureTests(unittest.TestCase):
    """Replays tests/fixtures/analytics scenarios (fixture 0.3 oracle, see docs/ADR_GOLDEN_FIXTURES_RESOLUTION.md).

    Coverage comes from the fixture's asserted ``source_coverage`` through an injected provider, exactly as a trusted
    server-side provider would supply it; without one every result is PARTIAL (tested elsewhere in this module)."""

    @classmethod
    def setUpClass(cls):
        base = ROOT / "tests/fixtures/analytics"
        cls.scenarios = {s["scenario_id"]: s for s in json.loads((base / "events.json").read_text("utf-8"))["scenarios"]}
        cls.expect = {e["scenario_id"]: e for e in json.loads((base / "expectations.json").read_text("utf-8"))["expectations"]}

    def replay(self, sid):
        s, e = self.scenarios[sid], self.expect[sid]
        types = {"DOOR_OPENED": "door_opened", "DOOR_FORCED_OPEN": "door_forced"}
        assets, events = {}, []
        for raw in s["raw_vendor_events"]:
            etype = types.get(raw["vendor_event_type"])
            if etype is None:
                continue  # ACCESS_GRANTED / UNLOCK_COMMAND etc. are not physical openings (D-01); not stored as door types
            ext = raw["vendor_asset_external_id"]
            aid = assets.setdefault(ext, len(assets) + 1)
            row = ev(raw["fixture_event_id"], raw["observed_at_utc"], etype, asset=aid, ident=raw["vendor_event_id"])
            row.update({"external_ids": [ext], "source_code": s["source"]["source_code"]})
            events.append(row)
        building = {"building_id": 1, "building_code": s["building"]["building_code"], "timezone": s["building"]["timezone"]}
        p = e["requested_parameters"]
        cov = s["source_coverage"]
        done = cov["status"] == "FIXTURE_ASSERTED_COMPLETE"
        a, b_, asof = (u(cov[k]) if cov.get(k) else None for k in ("covered_from_utc", "covered_to_utc", "data_as_of"))
        provider = lambda bld, st, en, codes: {c: SourceCoverage(c, True, done, a, b_, asof) for c in codes}
        env = dah.run_doors_after_hours(FakeCursor(building, events, [s["source"]["source_code"]]), building=p["building"],
                                        start=p["start"], end=p["end"], after_local=p.get("after_local"), coverage_provider=provider)
        self.ids = {x["fixture_event_id"]: x["fixture_event_id"] for x in s["raw_vendor_events"]}
        return env.to_dict(), e["expected"], e

    def counts(self, d):
        return {r["door_external_id"]: r["opening_count"] for r in d["rows"]}

    def test_S_A_boundary_counts_match(self):
        d, exp, _ = self.replay("S-A-AFTER-HOURS-BOUNDARY")
        self.assertEqual(self.counts(d), {r["door_external_id"]: r["opening_count"] for r in exp["rows"]})
        self.assertEqual([x["id"] for x in d["rows"][0]["evidence_refs"]], exp["rows"][0]["counted_evidence_refs"])

    def test_S_B_dst_counts_and_period_match(self):
        d, exp, e = self.replay("S-B-DST-CROSSING")
        self.assertEqual(self.counts(d), {r["door_external_id"]: r["opening_count"] for r in exp["rows"]})
        self.assertEqual(d["parameters"]["start_utc"], e["normalised_period"]["start_utc"])
        self.assertEqual(d["parameters"]["end_utc_exclusive"], e["normalised_period"]["end_utc_exclusive"])

    def test_S_D_no_physical_opening_gives_no_rows(self):
        d, exp, _ = self.replay("S-D-NO-PHYSICAL-OPENING")
        self.assertEqual((d["rows"], d["total_rows"]), ([], 0))
        self.assertEqual(exp["total_rows"], 0)

    def test_status_and_completeness_match_the_oracle_for_covered_scenarios(self):
        for sid in ("S-A-AFTER-HOURS-BOUNDARY", "S-B-DST-CROSSING", "S-D-NO-PHYSICAL-OPENING"):
            d, exp, _ = self.replay(sid)
            self.assertEqual((d["status"], d["complete"]), (exp["result_status_proposed"], exp["complete"]), sid)
            self.assertEqual(exp["result_status_proposed"], "COMPLETE")

    def test_S_E1_forced_open_is_not_counted_and_disclosed(self):                      # ADR D-01, G-DOOR-02 (fixture 0.3)
        d, exp, _ = self.replay("S-E1-DOOR-CORRELATED")
        want = exp["rows"][0]
        self.assertEqual(self.counts(d), {"EXT-DOOR-E1": want["opening_count"]})
        self.assertEqual([x["id"] for x in d["rows"][0]["evidence_refs"]], want["counted_evidence_refs"])
        self.assertEqual(d["rows"][0]["uncounted_door_forced_events"], want["uncounted_door_forced_events"])
        self.assertEqual((d["status"], d["complete"]), (exp["result_status_proposed"], exp["complete"]))
        self.assertEqual(sorted(x["code"] for x in d["limitations"]), sorted(exp["limitations"]))

    def test_S_E2_forced_open_is_not_counted_and_disclosed(self):                      # ADR D-01, G-DOOR-03 (fixture 0.3)
        d, exp, _ = self.replay("S-E2-DOOR-UNCORRELATED")
        want = exp["rows"][0]
        self.assertEqual(self.counts(d), {"EXT-DOOR-E2": want["opening_count"]})
        self.assertEqual(d["rows"][0]["uncounted_door_forced_events"], want["uncounted_door_forced_events"])
        self.assertEqual((d["status"], d["complete"]), (exp["result_status_proposed"], exp["complete"]))
        self.assertEqual(sorted(x["code"] for x in d["limitations"]), sorted(exp["limitations"]))

    def test_access_granted_and_unlock_command_are_never_openings(self):                # ADR D-01, G-DOOR-01
        d, exp, _ = self.replay("S-D-NO-PHYSICAL-OPENING")
        self.assertEqual((d["rows"], d["total_rows"]), ([], 0))


if __name__ == "__main__":
    unittest.main()
