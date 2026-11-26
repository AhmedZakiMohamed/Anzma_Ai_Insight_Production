"""devices.offline_frequency: session grouping, open-ended outages, per-device metrics, silence != outage, period/data
quality rules, evidence, and replay of the golden fixtures (tests/fixtures/analytics).

No database: a fake cursor answers the tool's static SQL from in-memory rows (including the look-back selection).
The SQL itself runs against PostgreSQL in tests/integration/test_devices_offline_frequency_sql.py.
"""
import json
import unittest
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from app.domain.tools import devices_offline_frequency as dof
from app.domain.tools.coverage import SourceCoverage
from app.domain.tools.envelope import ResultStatus
from app.domain.tools.periods import PeriodError, build_period, resolve_local
from app.domain.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc
NOW = datetime(2026, 11, 26, 0, 0, tzinfo=UTC)          # fixed "query time", after every fixture period
ONLINE, OFFLINE, UNKNOWN = dof.ONLINE, dof.OFFLINE, dof.UNKNOWN


def u(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _host_tzdb_has_cuba_2026f() -> bool:
    """tzdb 2026f cancelled Cuba's 2026-11-01 fall-back (Havana stays UTC-04). A host with an older tzdb still has the
    25-hour day, so tests that read the *real* Havana zone are skipped there instead of asserting the wrong rule.
    The period arithmetic itself is covered independently of the host tzdb by SyntheticTimezoneTests."""
    try:
        return datetime(2026, 11, 1, 12, tzinfo=ZoneInfo("America/Havana")).utcoffset() == timedelta(hours=-4)
    except Exception:  # missing zone data: treat as "cannot assert"
        return False


HOST_TZDB_HAS_CUBA_2026F = _host_tzdb_has_cuba_2026f()
SKIP_OLD_TZDB = "host IANA tzdb predates 2026f (Cuba fall-back cancelled); see SyntheticTimezoneTests for the host-independent check"


class FakeCursor:
    """Answers the tool's SQL from in-memory rows and records every execute call.

    ``rows`` are availability events (already filtered to the building). The look-back selection re-implements, in
    plain Python, what SQL_LOOKBACK_EVENTS does: per (asset, source) the last recovery, the first start after it and
    the last earlier event.
    """

    SYSTEMS = [{"code": "ACCESS_CONTROL", "eligible": False}, {"code": "CCTV", "eligible": True},
               {"code": "FIRE_ALARM", "eligible": False}, {"code": "HVAC", "eligible": False},
               {"code": "NETWORKING", "eligible": True}]

    def __init__(self, building, rows, sources=("SRC-A",), catalogue_columns=3):
        self.building, self.rows, self.sources = building, list(rows), list(sources)
        self.catalogue_columns = catalogue_columns
        self.calls, self._result = [], None

    def _scoped(self, params):
        systems, source = params["systems"], params["source"]
        return [r for r in self.rows if r["system_code"] in systems and (source is None or r["source_code"] == source)]

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql == dof.SQL_BUILDING:
            self._result = [self.building] if params["building_code"] == self.building["building_code"] else []
        elif sql == dof.SQL_CATALOGUE_READY:
            self._result = [{"n": self.catalogue_columns}]
        elif sql == dof.SQL_SYSTEMS:
            self._result = [dict(r) for r in self.SYSTEMS]
        elif sql == dof.SQL_SOURCES:
            self._result = [{"source_code": s} for s in sorted(self.sources) if params["source"] in (None, s)]
        elif sql == dof.SQL_PERIOD_EVENTS:
            self._result = sorted((r for r in self._scoped(params) if params["start_utc"] <= r["ts"] < params["end_utc"]),
                                  key=lambda r: (r["ts"], r["event_id"]))
        elif sql == dof.SQL_LOOKBACK_EVENTS:
            before = [r for r in self._scoped(params) if r["ts"] < params["start_utc"]]
            keep = set()
            for dev in {(r["asset_id"], r["source_id"]) for r in before}:
                evs = sorted((r for r in before if (r["asset_id"], r["source_id"]) == dev), key=lambda r: (r["ts"], r["event_id"]))
                closers = [r for r in evs if r["event_class"] == "FAULT_CLOSE"]
                anchor = closers[-1] if closers else None
                after = [r for r in evs if r["event_class"] == "FAULT_OPEN" and
                         (anchor is None or (r["ts"], r["event_id"]) > (anchor["ts"], anchor["event_id"]))]
                if anchor:
                    keep.add(anchor["event_id"])
                if after:
                    keep.add(after[0]["event_id"])
                keep.add(evs[-1]["event_id"])
            self._result = sorted((r for r in before if r["event_id"] in keep), key=lambda r: (r["ts"], r["event_id"]))
        else:
            raise AssertionError("unexpected SQL")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


def ev(i, ts, kind, asset=1, source_id=1, ident="auto", system="CCTV"):
    """kind: 'off' | 'on'. CCTV uses camera_*, NETWORKING uses link_*."""
    names = {"CCTV": ("camera_offline", "camera_online"), "NETWORKING": ("link_down", "link_up")}[system]
    return {"event_id": i, "ts": u(ts), "event_type": names[0] if kind == "off" else names[1],
            "event_class": "FAULT_OPEN" if kind == "off" else "FAULT_CLOSE", "source_id": source_id,
            "source_code": f"SRC-{source_id}" if source_id is not None else None,
            "source_event_id": f"E{i}" if ident == "auto" else ident, "asset_id": asset, "asset_code": f"CAM-{asset}",
            "asset_name": f"Camera {asset}", "system_code": system, "external_ids": [f"EXT-{asset}"]}


CAIRO = {"building_id": 1, "building_code": "BLD-A", "timezone": "Africa/Cairo"}
DAY = dict(start="2026-11-02", end="2026-11-02")           # Cairo: [2026-11-01T22:00Z, 2026-11-02T22:00Z)
P0, P1 = u("2026-11-01T22:00:00Z"), u("2026-11-02T22:00:00Z")


def covered(data_as_of=None, confirmed=True, supports=True, covered_to=None):
    def provider(building, start, end, codes):
        return {c: SourceCoverage(c, supports, confirmed, start if confirmed else None,
                                  covered_to or end if confirmed else None, data_as_of or end) for c in codes}
    return provider


def run(rows, provider=None, building=CAIRO, sources=("SRC-1",), confirmed=frozenset(), **kw):
    args = dict(building=building["building_code"], **DAY)
    args.update(kw)
    cur = FakeCursor(building, rows, sources)
    env = dof.run_devices_offline_frequency(cur, coverage_provider=provider, confirmed_timezone_buildings=confirmed,
                                            clock=lambda: NOW, **args)
    return env.to_dict()


def codes(d):
    return [x["code"] for x in d["limitations"]]


def row(d, i=0):
    return d["rows"][i]


class StateMachineTests(unittest.TestCase):
    """analyse_events: the pure core (ADR D-06..D-08)."""

    def analyse(self, events, start=P0):
        return dof.analyse_events(events, start)

    def test_repeated_offline_is_one_session(self):                                   # G-OFF-03
        (dev,) = self.analyse([ev(1, "2026-11-02T08:00:00Z", "off"), ev(2, "2026-11-02T08:05:00Z", "off"),
                               ev(3, "2026-11-02T08:10:00Z", "off"), ev(4, "2026-11-02T08:20:00Z", "on")])
        self.assertEqual(len(dev.sessions), 1)
        self.assertEqual((dev.sessions[0].start, dev.sessions[0].end), (u("2026-11-02T08:00:00Z"), u("2026-11-02T08:20:00Z")))
        self.assertEqual([r.id for r in dev.sessions[0].refs], ["1", "2", "3", "4"])
        self.assertEqual(dev.sessions[0].openers, 3)

    def test_cycles_are_never_merged_even_when_close(self):                           # G-OFF-06 / G-OFF-07
        events = [ev(i + 1, f"2026-11-02T09:0{m}:00Z", k) for i, (m, k) in
                  enumerate([(0, "off"), (2, "on"), (3, "off"), (4, "on"), (6, "off"), (7, "on")])]
        (dev,) = self.analyse(events)
        self.assertEqual(len(dev.sessions), 3)

    def test_end_without_start_is_orphan_not_a_session(self):                         # G-OFF-04
        (dev,) = self.analyse([ev(1, "2026-11-02T12:00:00Z", "on")])
        self.assertEqual((len(dev.sessions), [r.id for r in dev.orphans], dev.initial_state), (0, ["1"], UNKNOWN))
        self.assertEqual(dev.in_period_events, 1)

    def test_start_without_end_stays_open(self):                                      # G-OFF-05
        (dev,) = self.analyse([ev(1, "2026-11-02T20:00:00Z", "off")])
        self.assertIsNone(dev.sessions[0].end)

    def test_lookback_state_offline_then_online_and_unknown(self):                    # D-06 initial state
        offline = self.analyse([ev(1, "2026-11-01T20:00:00Z", "off")])[0]
        online = self.analyse([ev(1, "2026-11-01T10:00:00Z", "off"), ev(2, "2026-11-01T11:00:00Z", "on"),
                               ev(3, "2026-11-02T12:00:00Z", "on")])[0]
        unknown = self.analyse([ev(1, "2026-11-02T05:00:00Z", "off")])[0]
        self.assertEqual((offline.initial_state, online.initial_state, unknown.initial_state), (OFFLINE, ONLINE, UNKNOWN))
        self.assertEqual(len(online.orphans), 1)                  # recovered earlier, so this second end is an orphan
        self.assertEqual([r.id for r in online.initial_refs], ["1", "2"])

    def test_pre_period_orphan_is_not_counted(self):
        (dev,) = self.analyse([ev(1, "2026-11-01T11:00:00Z", "on"), ev(2, "2026-11-02T05:00:00Z", "off")])
        self.assertEqual((dev.orphans, dev.initial_state), ([], ONLINE))

    def test_duplicate_deliveries_collapse_and_missing_identity_is_kept(self):        # G-DUP-01 / G-DUP-02
        dup = [ev(1, "2026-11-02T10:00:00Z", "off", ident="X"), ev(2, "2026-11-02T10:00:00Z", "off", ident="X"),
               ev(3, "2026-11-02T10:30:00Z", "on", ident="Y"), ev(4, "2026-11-02T10:30:00Z", "on", ident="Y")]
        (d1,) = self.analyse(dup)
        self.assertEqual((d1.duplicates, len(d1.sessions), d1.orphans), (2, 1, []))   # a duplicate end is not an orphan
        (d2,) = self.analyse([ev(1, "2026-11-02T10:00:00Z", "off", ident=None), ev(2, "2026-11-02T10:00:00Z", "off", ident=None),
                              ev(3, "2026-11-02T10:30:00Z", "on", ident=None)])
        self.assertEqual((d2.missing_identity, d2.duplicates, len(d2.sessions[0].refs)), (3, 0, 3))

    def test_same_instant_orders_by_event_id(self):
        (dev,) = self.analyse([ev(2, "2026-11-02T10:00:00Z", "on"), ev(1, "2026-11-02T10:00:00Z", "off")])
        self.assertEqual((len(dev.sessions), dev.sessions[0].end), (1, u("2026-11-02T10:00:00Z")))   # zero-length session

    def test_devices_are_separated_by_asset_and_source(self):
        devs = self.analyse([ev(1, "2026-11-02T10:00:00Z", "off", asset=1), ev(2, "2026-11-02T10:10:00Z", "on", asset=2),
                             ev(3, "2026-11-02T10:20:00Z", "on", asset=1, source_id=2)])
        self.assertEqual(len(devs), 3)
        self.assertEqual(sum(len(d.orphans) for d in devs), 2)   # another asset's / source's recovery never closes asset 1

    def test_unknown_event_class_is_ignored(self):
        bad = dict(ev(1, "2026-11-02T10:00:00Z", "off"), event_class="ACTIVITY")
        self.assertEqual(self.analyse([bad]), [])



class SessionMetricsTests(unittest.TestCase):
    def test_per_device_metrics_and_session_durations(self):
        rows = [ev(1, "2026-11-02T10:00:00Z", "off"), ev(2, "2026-11-02T10:30:00Z", "on"),
                ev(3, "2026-11-02T12:00:00Z", "off"), ev(4, "2026-11-02T12:10:00Z", "on")]
        d = run(rows, covered())
        r = row(d)
        self.assertEqual((r["outages_started_in_period"], r["downtime_seconds_in_period"], r["flapping"]), (2, 2400, "not_evaluated"))
        self.assertEqual([(s["overlap_seconds"], s["duration_seconds"]) for s in r["sessions"]], [(1800, 1800), (600, 600)])
        self.assertEqual([(s["open"], s["duration_is_lower_bound"], s["recovery_observed"]) for s in r["sessions"]], [(False, False, True)] * 2)
        self.assertEqual((r["open_at_period_end"], r["duration_is_lower_bound"], r["initial_state"]), (False, False, "UNKNOWN"))
        self.assertEqual([x["id"] for x in r["evidence_refs"]], ["1", "2", "3", "4"])
        self.assertEqual([x["id"] for x in r["sessions"][1]["evidence_refs"]], ["3", "4"])
        self.assertEqual(r["sessions"][0]["evidence_refs"][0], {"kind": "event_record", "id": "1", "observed_at":
                                                                "2026-11-02T10:00:00Z", "event_type": "camera_offline"})
        self.assertEqual((r["asset_external_id"], r["source_code"], r["building_code"]), ("EXT-1", "SRC-1", "BLD-A"))

    def test_merged_alarms_are_reported_on_the_session(self):
        d = run([ev(1, "2026-11-02T08:00:00Z", "off"), ev(2, "2026-11-02T08:05:00Z", "off"),
                 ev(3, "2026-11-02T08:10:00Z", "off"), ev(4, "2026-11-02T08:20:00Z", "on")], covered())
        s = row(d)["sessions"][0]
        self.assertEqual((row(d)["outages_started_in_period"], s["repeated_start_events"], s["overlap_seconds"]), (1, 2, 1200))

    def test_session_started_before_period_counts_only_the_overlap(self):              # G-OFF-08
        d = run([ev(1, "2026-11-01T20:00:00Z", "off"), ev(2, "2026-11-02T01:00:00Z", "on")], covered())
        r = row(d)
        self.assertEqual((r["initial_state"], r["outages_started_in_period"], r["outages_ongoing_at_period_start"],
                          r["downtime_seconds_in_period"]), ("OFFLINE", 0, 1, 10800))
        s = r["sessions"][0]
        self.assertEqual((s["started_in_period"], s["ongoing_at_period_start"], s["overlap_start_utc"], s["duration_seconds"]),
                         (False, True, "2026-11-01T22:00:00Z", 18000))        # full duration 5 h, overlap 3 h
        self.assertEqual(codes(d), ["FLAPPING_NOT_EVALUATED"])

    def test_device_down_for_the_whole_period_without_any_in_period_event_gets_a_row(self):
        d = run([ev(1, "2026-10-01T00:00:00Z", "off"), ev(2, "2026-10-01T00:05:00Z", "off")], covered())
        r = row(d)
        self.assertEqual((d["total_rows"], r["outages_started_in_period"], r["outages_ongoing_at_period_start"],
                          r["downtime_seconds_in_period"], r["open_at_period_end"]), (1, 0, 1, 86400, True))
        self.assertEqual(r["sessions"][0]["start_utc"], "2026-10-01T00:00:00Z")   # the FIRST start, not the repeat
        self.assertIn("OPEN_OUTAGE_LOWER_BOUND", codes(d))

    def test_recovered_device_with_no_in_period_event_has_no_row(self):
        d = run([ev(1, "2026-10-01T00:00:00Z", "off"), ev(2, "2026-10-01T01:00:00Z", "on")], covered())
        self.assertEqual((d["rows"], d["status"]), ([], "COMPLETE"))              # absent, not a reported zero

    def test_session_that_ended_exactly_at_period_start_does_not_overlap(self):
        d = run([ev(1, "2026-11-01T20:00:00Z", "off"), ev(2, "2026-11-01T22:00:00Z", "on")], covered())
        r = row(d)
        self.assertEqual((r["sessions"], r["outages_ongoing_at_period_start"], r["downtime_seconds_in_period"]), ([], 0, 0))

    def test_no_earlier_transition_means_unknown_state_and_nothing_assumed(self):      # G-OFF-09
        d = run([ev(1, "2026-11-02T05:00:00Z", "off"), ev(2, "2026-11-02T05:30:00Z", "on")], covered())
        r = row(d)
        self.assertEqual((r["initial_state"], r["outages_ongoing_at_period_start"], r["downtime_seconds_in_period"]), ("UNKNOWN", 0, 1800))
        self.assertEqual(sorted(codes(d)), ["FLAPPING_NOT_EVALUATED", "INITIAL_STATE_UNKNOWN"])
        self.assertEqual(d["limitations"][0]["details"], {"devices": 1})

    def test_orphan_end_is_reported_with_refs(self):                                   # G-OFF-04
        d = run([ev(1, "2026-11-01T10:00:00Z", "off"), ev(2, "2026-11-01T11:00:00Z", "on"), ev(3, "2026-11-02T12:00:00Z", "on")], covered())
        r = row(d)
        self.assertEqual((r["initial_state"], r["sessions"], r["orphan_offline_end_count"], r["downtime_seconds_in_period"]), ("ONLINE", [], 1, 0))
        self.assertEqual([x["id"] for x in r["orphan_offline_end_refs"]], ["3"])
        self.assertIn("ORPHAN_OFFLINE_END", codes(d))

    def test_missing_identity_flag(self):
        d = run([ev(1, "2026-11-02T10:00:00Z", "off", ident=None), ev(2, "2026-11-02T10:30:00Z", "on", ident=None)], covered())
        lim = {x["code"]: x for x in d["limitations"]}
        self.assertEqual(lim["MISSING_EVENT_IDENTITY"]["details"], {"events_without_identity": 2})
        self.assertEqual(row(d)["outages_started_in_period"], 1)                # still counted individually, never merged by similarity

    def test_duplicates_are_reported_per_device(self):                                 # G-DUP-01
        d = run([ev(1, "2026-11-02T10:00:00Z", "off", ident="X"), ev(2, "2026-11-02T10:00:00Z", "off", ident="X"),
                 ev(3, "2026-11-02T10:30:00Z", "on", ident="Y")], covered())
        self.assertEqual((row(d)["duplicate_events_ignored"], row(d)["outages_started_in_period"], len(row(d)["evidence_refs"])), (1, 1, 2))
        self.assertNotIn("MISSING_EVENT_IDENTITY", codes(d))


class OpenEndedOutageTests(unittest.TestCase):
    def test_open_outage_measured_to_period_end_when_data_as_of_is_later(self):       # G-OFF-11
        d = run([ev(1, "2026-11-02T20:00:00Z", "off")], covered(data_as_of=u("2026-11-03T01:00:00Z")))
        r, s = row(d), row(d)["sessions"][0]
        self.assertEqual((r["downtime_seconds_in_period"], r["open_at_period_end"], r["duration_is_lower_bound"]), (7200, True, True))
        self.assertEqual((s["open"], s["end_utc"], s["duration_seconds"], s["recovery_observed"], s["duration_is_lower_bound"]),
                         (True, None, None, False, True))
        self.assertEqual((s["cutoff_utc"], s["cutoff_reason"], s["overlap_end_utc"]), ("2026-11-02T22:00:00Z", "period_end", "2026-11-02T22:00:00Z"))
        self.assertEqual((d["status"], d["complete"]), ("COMPLETE", True))
        lim = {x["code"]: x for x in d["limitations"]}["OPEN_OUTAGE_LOWER_BOUND"]
        self.assertEqual(lim["details"], {"devices": 1, "open_sessions": 1})

    def test_open_outage_measured_to_data_as_of_when_it_is_earlier(self):             # G-OFF-10
        d = run([ev(1, "2026-11-02T20:00:00Z", "off")], covered(data_as_of=u("2026-11-02T21:00:00Z"), confirmed=False))
        s = row(d)["sessions"][0]
        self.assertEqual((row(d)["downtime_seconds_in_period"], s["cutoff_utc"], s["cutoff_reason"]), (3600, "2026-11-02T21:00:00Z", "data_as_of"))
        self.assertEqual((d["status"], d["complete"]), ("PARTIAL", False))
        self.assertIn("COMPLETION_NOT_CONFIRMED", codes(d))

    def test_open_outage_never_extends_into_the_future(self):
        """The requested period has not ended yet and nothing reports data_as_of: the query time bounds the duration."""
        cur = FakeCursor(CAIRO, [ev(1, "2026-11-02T20:00:00Z", "off")])
        env = dof.run_devices_offline_frequency(cur, building="BLD-A", **DAY, clock=lambda: u("2026-11-02T21:30:00Z")).to_dict()
        s = row(env)["sessions"][0]
        self.assertEqual((row(env)["downtime_seconds_in_period"], s["cutoff_reason"]), (5400, "query_time"))

    def test_unknown_data_as_of_falls_back_to_period_end_and_stays_partial(self):
        d = run([ev(1, "2026-11-02T20:00:00Z", "off")])
        self.assertEqual((row(d)["sessions"][0]["cutoff_reason"], d["status"], d["data_as_of"]), ("period_end", "PARTIAL", None))
        self.assertIn("DATA_AS_OF_UNKNOWN", codes(d))

    def test_open_session_from_before_the_period_is_cut_at_the_same_bound(self):
        d = run([ev(1, "2026-11-01T20:00:00Z", "off")], covered(data_as_of=u("2026-11-02T00:00:00Z"), confirmed=False))
        r = row(d)
        self.assertEqual((r["downtime_seconds_in_period"], r["outages_ongoing_at_period_start"], r["outages_started_in_period"]), (7200, 1, 0))

    def test_data_as_of_before_session_start_gives_zero_not_negative(self):
        d = run([ev(1, "2026-11-02T20:00:00Z", "off")], covered(data_as_of=u("2026-11-02T19:00:00Z"), confirmed=False))
        self.assertEqual((row(d)["downtime_seconds_in_period"], row(d)["sessions"][0]["overlap_end_utc"]), (0, "2026-11-02T20:00:00Z"))

    def test_recovery_after_period_end_is_not_seen_and_session_stays_open(self):
        d = run([ev(1, "2026-11-02T21:00:00Z", "off"), ev(2, "2026-11-02T22:00:00Z", "on")], covered())   # end == exclusive bound
        self.assertEqual((row(d)["open_at_period_end"], row(d)["downtime_seconds_in_period"]), (True, 3600))


class SilenceIsNotAnOutageTests(unittest.TestCase):
    def test_silence_produces_no_rows(self):                                           # G-OFF-02
        d = run([], covered())
        self.assertEqual((d["status"], d["complete"], d["rows"], d["total_rows"], d["limitations"]), ("COMPLETE", True, [], 0, []))

    def test_source_silence_with_a_coverage_gap_is_partial_and_still_has_no_outage(self):
        gap = covered(data_as_of=u("2026-11-02T06:00:00Z"), covered_to=u("2026-11-02T06:00:00Z"))
        d = run([], gap)
        self.assertEqual((d["status"], d["rows"], d["total_rows"]), ("PARTIAL", [], 0))
        self.assertEqual(codes(d), ["COMPLETION_NOT_CONFIRMED", "DATA_AS_OF_UNKNOWN"])   # unconfirmed coverage gives no envelope data_as_of

    def test_no_provider_means_partial_never_complete_zero(self):
        d = run([])
        self.assertEqual((d["status"], d["complete"], d["total_rows"]), ("PARTIAL", False, 0))

    def test_heartbeat_and_fault_events_are_not_read(self):
        cur = FakeCursor(CAIRO, [])
        dof.run_devices_offline_frequency(cur, building="BLD-A", **DAY, clock=lambda: NOW)
        for sql in (dof.SQL_PERIOD_EVENTS, dof.SQL_LOOKBACK_EVENTS):
            self.assertIn("outage_class = 'AVAILABILITY'", sql)
            self.assertNotIn("heartbeat", sql)
            self.assertNotIn("hvac_fault", sql)


class SystemScopeTests(unittest.TestCase):
    def test_fault_only_systems_are_unsupported_not_zero(self):                       # G-OFF-01
        d = run([], covered(), systems=["HVAC", "FIRE_ALARM"])
        self.assertEqual((d["status"], d["rows"], d["complete"]), ("UNSUPPORTED", [], False))
        self.assertEqual(d["limitations"][0]["code"], "SYSTEM_NOT_ELIGIBLE_FOR_OFFLINE")
        self.assertEqual(d["limitations"][0]["details"]["unsupported_systems"], ["FIRE_ALARM", "HVAC"])
        self.assertEqual(d["parameters"]["systems"], ["FIRE_ALARM", "HVAC"])

    def test_mixed_request_returns_eligible_part_as_partial(self):
        d = run([ev(1, "2026-11-02T10:00:00Z", "off"), ev(2, "2026-11-02T10:30:00Z", "on")], covered(), systems=["cctv", "HVAC"])
        self.assertEqual((d["status"], d["total_rows"]), ("PARTIAL", 1))
        self.assertIn("SYSTEM_NOT_ELIGIBLE_FOR_OFFLINE", codes(d))

    def test_unknown_system_is_cannot_compute(self):
        d = run([], covered(), systems=["TOASTERS"])
        self.assertEqual((d["status"], d["limitations"][0]["code"]), ("CANNOT_COMPUTE", "INVALID_PARAMETER"))

    def test_systems_and_source_filters(self):
        rows = [ev(1, "2026-11-02T10:00:00Z", "off", asset=1, source_id=1), ev(2, "2026-11-02T10:30:00Z", "on", asset=1, source_id=1),
                ev(3, "2026-11-02T11:00:00Z", "off", asset=2, source_id=2, system="NETWORKING")]
        everything = run(rows, covered(), sources=("SRC-1", "SRC-2"))
        self.assertEqual(sorted(r["system_type"] for r in everything["rows"]), ["CCTV", "NETWORKING"])
        self.assertEqual([r["system_type"] for r in run(rows, covered(), sources=("SRC-1", "SRC-2"), systems=["NETWORKING"])["rows"]], ["NETWORKING"])
        only = run(rows, covered(), sources=("SRC-1", "SRC-2"), source="SRC-1")
        self.assertEqual(([r["source_code"] for r in only["rows"]], only["source_ids"]), (["SRC-1"], ["SRC-1"]))

    def test_catalogue_without_migration_014_is_cannot_compute(self):
        cur = FakeCursor(CAIRO, [], catalogue_columns=0)
        d = dof.run_devices_offline_frequency(cur, building="BLD-A", **DAY, clock=lambda: NOW).to_dict()
        self.assertEqual((d["status"], d["limitations"][0]["code"]), ("CANNOT_COMPUTE", "CATALOGUE_NOT_MIGRATED"))
        self.assertEqual(len(cur.calls), 2)        # stopped before touching event_records


class RankingAndTruncationTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            ev(1, "2026-11-02T02:00:00Z", "off", asset=1, source_id=2, system="NETWORKING"), ev(2, "2026-11-02T02:10:00Z", "on", asset=1, source_id=2, system="NETWORKING"),
            ev(3, "2026-11-02T03:00:00Z", "off", asset=1, source_id=2, system="NETWORKING"), ev(4, "2026-11-02T03:05:00Z", "on", asset=1, source_id=2, system="NETWORKING"),
            ev(5, "2026-11-02T05:00:00Z", "off", asset=2, source_id=1), ev(6, "2026-11-02T05:02:00Z", "on", asset=2, source_id=1),
            ev(7, "2026-11-02T07:00:00Z", "off", asset=3, source_id=2), ev(8, "2026-11-02T07:04:00Z", "on", asset=3, source_id=2),
        ]
        self.sources = ("SRC-1", "SRC-2")

    def test_default_order_is_outage_count_then_identity(self):
        d = run(self.rows, covered(), sources=self.sources)
        self.assertEqual([(r["asset_code"], r["outages_started_in_period"]) for r in d["rows"]], [("CAM-1", 2), ("CAM-2", 1), ("CAM-3", 1)])
        self.assertEqual(d["parameters"]["rank_by"], "outages_started")        # SRC-1 sorts before SRC-2 on the tie

    def test_rank_by_downtime(self):
        d = run(self.rows, covered(), sources=self.sources, rank_by="downtime_seconds")
        self.assertEqual([(r["asset_code"], r["downtime_seconds_in_period"]) for r in d["rows"]], [("CAM-1", 900), ("CAM-3", 240), ("CAM-2", 120)])

    def test_invalid_rank_by(self):
        self.assertEqual(run([], covered(), rank_by="vibes")["status"], "CANNOT_COMPUTE")

    def test_truncation_is_explicit(self):
        cur = FakeCursor(CAIRO, self.rows, self.sources)
        d = dof.run_devices_offline_frequency(cur, building="BLD-A", **DAY, coverage_provider=covered(), max_rows=2, clock=lambda: NOW).to_dict()
        self.assertEqual((len(d["rows"]), d["total_rows"], d["truncated"], d["complete"]), (2, 3, True, False))
        self.assertIn("ROWS_TRUNCATED", codes(d))

    def test_ordering_is_deterministic_across_input_order(self):
        a = run(self.rows, covered(), sources=self.sources)
        b = run(list(reversed(self.rows)), covered(), sources=self.sources)
        self.assertEqual(a["rows"], b["rows"])
        self.assertEqual(a["result_id"], b["result_id"])


class PeriodAndQualityTests(unittest.TestCase):
    def test_missing_or_invalid_timezone_is_cannot_compute(self):                      # G-TZ-01
        for tz in (None, "", "Fixture/Not_A_Zone"):
            d = run([ev(1, "2026-11-02T10:00:00Z", "off")], covered(), building=dict(CAIRO, timezone=tz))
            self.assertEqual((d["status"], d["rows"], d["limitations"][0]["code"]), ("CANNOT_COMPUTE", [], "TIMEZONE_UNAVAILABLE"))

    def test_timestamp_without_offset_is_rejected(self):                               # G-TZ-02
        d = run([], covered(), start="2026-11-02T00:00:00", end="2026-11-03T00:00:00")
        self.assertEqual((d["status"], d["limitations"][0]["code"]), ("CANNOT_COMPUTE", "INVALID_PARAMETER"))

    def test_bad_inputs_and_unknown_building(self):
        for kw in ({"start": "2026-11-05"}, {"start": "2026-02-30", "end": "2026-03-01"}, {"start": "2026-01-01", "end": "2027-06-01"},
                   {"start": "2026-11-02", "end": "2026-11-02T00:00:00Z"}):
            d = run([], covered(), **kw)
            self.assertEqual((d["status"], d["rows"]), ("CANNOT_COMPUTE", []), kw)
        cur = FakeCursor(CAIRO, [])
        d = dof.run_devices_offline_frequency(cur, building="BLD-X", **DAY, clock=lambda: NOW).to_dict()
        self.assertEqual((d["status"], d["limitations"][0]["code"], len(cur.calls)), ("CANNOT_COMPUTE", "INVALID_PARAMETER", 1))

    def test_explicit_offset_period_is_half_open(self):                                # G-PER-01
        rows = [ev(1, "2026-11-01T22:00:00Z", "off"), ev(2, "2026-11-01T22:10:00Z", "on"), ev(3, "2026-11-02T22:00:00Z", "off")]
        d = run(rows, covered(), start="2026-11-02T00:00:00+02:00", end="2026-11-03T00:00:00+02:00")
        r = row(d)
        self.assertEqual((r["outages_started_in_period"], r["downtime_seconds_in_period"], r["open_at_period_end"]), (1, 600, False))
        self.assertEqual([x["id"] for x in r["evidence_refs"]], ["1", "2"])        # the event exactly at `end` is excluded
        self.assertEqual((d["parameters"]["start_utc"], d["parameters"]["end_utc_exclusive"]), ("2026-11-01T22:00:00Z", "2026-11-02T22:00:00Z"))

    def test_date_period_normalisation_is_echoed(self):                                # G-PER-02
        d = run([], covered(), start="2026-11-01", end="2026-11-07")
        p = d["parameters"]
        self.assertEqual((p["timezone"], p["start_utc"], p["end_utc_exclusive"]), ("Africa/Cairo", "2026-10-31T22:00:00Z", "2026-11-07T22:00:00Z"))

    def test_dst_missing_local_midnight_uses_first_valid_instant(self):                # fixture S-T1, tzdata
        p = build_period("2026-03-08", "2026-03-08", "America/Havana")
        self.assertEqual((p.start_utc, p.end_utc_exclusive), (u("2026-03-08T05:00:00Z"), u("2026-03-09T04:00:00Z")))

    @unittest.skipUnless(HOST_TZDB_HAS_CUBA_2026F, SKIP_OLD_TZDB)
    def test_havana_2026f_cancelled_fallback_is_24_hours(self):                         # fixture S-T2, IANA tzdb 2026f
        p = build_period("2026-11-01", "2026-11-01", "America/Havana")
        self.assertEqual((p.start_utc, p.end_utc_exclusive), (u("2026-11-01T04:00:00Z"), u("2026-11-02T04:00:00Z")))

    def test_synthetic_ambiguous_midnight_chooses_first_occurrence_and_spans_25_hours(self):
        # A tiny explicit tzinfo makes this rule independent of the host's IANA database version.
        class SyntheticFallBack(tzinfo):
            key = "TaskTest/Synthetic-Fall-Back"

            def utcoffset(self, value):
                if value is not None and (value.year, value.month, value.day, value.hour) == (2026, 11, 1, 0):
                    return timedelta(hours=-4 if value.fold == 0 else -5)
                return timedelta(hours=-5)

            def dst(self, value):
                return timedelta(0)

            def tzname(self, value):
                return "SYNTHETIC"

        zone = SyntheticFallBack()
        first = resolve_local(date(2026, 11, 1), time(0, 0), zone, midnight_rule=True)
        second_occurrence = datetime(2026, 11, 1, 0, 0, tzinfo=zone, fold=1).astimezone(UTC)
        end = resolve_local(date(2026, 11, 2), time(0, 0), zone, midnight_rule=True)
        self.assertEqual(first, u("2026-11-01T04:00:00Z"))
        self.assertEqual(second_occurrence, u("2026-11-01T05:00:00Z"))
        self.assertEqual(end, u("2026-11-02T05:00:00Z"))
        self.assertEqual(end - first, timedelta(hours=25))

    def test_build_period_errors(self):
        for tz in (None, "", "Mars/Olympus"):
            with self.assertRaises(PeriodError) as cm:
                build_period("2026-11-01", "2026-11-01", tz)
            self.assertEqual(cm.exception.code, "TIMEZONE_UNAVAILABLE")

    def test_backfill_timezone_is_never_complete_unless_operator_confirmed(self):
        gmt2 = dict(CAIRO, timezone="Etc/GMT-2")
        d = run([], covered(), building=gmt2)
        self.assertEqual((d["status"], codes(d)), ("PARTIAL", ["TIMEZONE_PROVENANCE_UNCONFIRMED"]))
        self.assertEqual(run([], covered(), building=gmt2, confirmed=frozenset({"BLD-A"}))["status"], "COMPLETE")

    def test_capability_unsupported_is_not_zero(self):                                 # G-COMP-02
        d = run([ev(1, "2026-11-02T10:00:00Z", "off")], covered(supports=False))
        self.assertEqual((d["status"], d["rows"], d["limitations"][0]["code"]), ("UNSUPPORTED", [], "CAPABILITY_UNSUPPORTED"))

    def test_page_failure_style_partial_keeps_rows(self):                              # G-COMP-01 / G-COMP-04
        d = run([ev(1, "2026-11-02T10:00:00Z", "off"), ev(2, "2026-11-02T10:30:00Z", "on")], covered(confirmed=False))
        self.assertEqual((d["status"], d["complete"], d["total_rows"]), ("PARTIAL", False, 1))
        self.assertIn("COMPLETION_NOT_CONFIRMED", codes(d))

    def test_complete_requires_trusted_provider_and_gives_data_as_of(self):
        d = run([ev(1, "2026-11-02T10:00:00Z", "off"), ev(2, "2026-11-02T10:30:00Z", "on")], covered())
        self.assertEqual((d["status"], d["data_as_of"], d["source_ids"]), ("COMPLETE", "2026-11-02T22:00:00Z", ["SRC-1"]))

    def test_no_sources_mapped_is_not_complete(self):
        d = run([], covered(), sources=())
        self.assertEqual(d["status"], "PARTIAL")
        self.assertIn("no device sources are mapped", d["limitations"][0]["details"]["reason"])


class ToolWiringTests(unittest.TestCase):
    def make_registry(self, cur, **kw):
        @contextmanager
        def factory():
            yield cur
        return ToolRegistry([dof.build_devices_offline_frequency_spec(factory, clock=lambda: NOW, **kw)])

    def test_registered_tool_runs_through_registry_and_model_name(self):
        reg = self.make_registry(FakeCursor(CAIRO, [ev(1, "2026-11-02T10:00:00Z", "off"), ev(2, "2026-11-02T10:30:00Z", "on")]))
        self.assertEqual(reg.agent_tools()[0]["name"], "devices__offline_frequency")
        d = reg.call("devices__offline_frequency", {"building": "BLD-A", **DAY}).to_dict()
        self.assertEqual((d["tool"], d["query_version"], row(d)["outages_started_in_period"]), ("devices.offline_frequency", "1", 1))

    def test_registers_next_to_doors_after_hours(self):
        from app.domain.tools import doors_after_hours as dah
        cur = FakeCursor(CAIRO, [])

        @contextmanager
        def factory():
            yield cur
        reg = ToolRegistry([dah.build_doors_after_hours_spec(factory), dof.build_devices_offline_frequency_spec(factory)])
        self.assertEqual(reg.names(), ["devices.offline_frequency", "doors.after_hours"])

    def test_caller_cannot_claim_completeness_or_lift_the_cap(self):
        reg = self.make_registry(FakeCursor(CAIRO, []))
        for extra in ({"complete": True}, {"coverage_provider": "x"}, {"confirmed_timezone_buildings": ["BLD-A"]}, {"max_rows": 10**6}, {"clock": "now"}):
            env = reg.call("devices.offline_frequency", {"building": "BLD-A", **DAY, **extra})
            self.assertEqual(env.status, ResultStatus.CANNOT_COMPUTE, extra)

    def test_structural_validation_of_arguments(self):
        reg = self.make_registry(FakeCursor(CAIRO, []))
        self.assertEqual(reg.call("devices.offline_frequency", {"building": "BLD-A", **DAY, "systems": "CCTV"}).status, ResultStatus.CANNOT_COMPUTE)
        d = reg.call("devices.offline_frequency", {"building": "BLD-A", **DAY, "systems": [1, 2]}).to_dict()
        self.assertEqual((d["status"], d["limitations"][0]["code"]), ("CANNOT_COMPUTE", "INVALID_PARAMETER"))

    def test_trusted_row_cap_applies(self):
        rows = [ev(i * 2 - 1, "2026-11-02T10:00:00Z", "off", asset=i) for i in (1, 2, 3)]
        reg = self.make_registry(FakeCursor(CAIRO, rows), max_rows=1)
        d = reg.call("devices.offline_frequency", {"building": "BLD-A", **DAY}).to_dict()
        self.assertEqual((len(d["rows"]), d["total_rows"], d["truncated"]), (1, 3, True))

    def test_sql_is_static_and_fully_parameterised(self):
        cur = FakeCursor(CAIRO, [ev(1, "2026-11-02T10:00:00Z", "off")])
        evil = "BLD-A'; DROP TABLE event_records;--"
        dof.run_devices_offline_frequency(cur, building=evil, **DAY, clock=lambda: NOW)
        dof.run_devices_offline_frequency(cur, building="BLD-A", **DAY, source="x'; DELETE FROM assets;--", clock=lambda: NOW)
        dof.run_devices_offline_frequency(cur, building="BLD-A", **DAY, clock=lambda: NOW)
        for sql, params in cur.calls:
            self.assertNotIn("DROP", sql)
            self.assertNotIn("DELETE", sql)
            self.assertNotIn("BLD-A", sql)
            self.assertTrue(params is None or isinstance(params, dict))
        statements = (dof.SQL_BUILDING, dof.SQL_CATALOGUE_READY, dof.SQL_SYSTEMS, dof.SQL_SOURCES, dof.SQL_PERIOD_EVENTS, dof.SQL_LOOKBACK_EVENTS)
        for sql in statements:
            self.assertTrue(sql.lstrip().upper().startswith(("SELECT", "WITH")))
            for verb in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE"):
                self.assertNotRegex(sql.upper(), rf"\b{verb}\b")


class GoldenFixtureTests(unittest.TestCase):
    """Replays the devices.offline_frequency scenarios of tests/fixtures/analytics (fixtures are NOT edited).

    Differences between the fixture oracle and this implementation are asserted explicitly and listed in
    docs/DEVICES_OFFLINE_FREQUENCY.md.
    """

    LIMITATION_LABELS = {"LEGACY_NULL_SOURCE_EVENT_ID": "MISSING_EVENT_IDENTITY", "PAGINATION_INCOMPLETE": "COMPLETION_NOT_CONFIRMED",
                         "COVERAGE_ENDS_BEFORE_PERIOD_END": "COMPLETION_NOT_CONFIRMED",
                         "SOURCE_SILENT_COVERAGE_GAP": "COMPLETION_NOT_CONFIRMED"}

    @classmethod
    def setUpClass(cls):
        base = ROOT / "tests/fixtures/analytics"
        cls.scenarios = {s["scenario_id"]: s for s in json.loads((base / "events.json").read_text("utf-8"))["scenarios"]}
        cls.expect = {e["scenario_id"]: e for e in json.loads((base / "expectations.json").read_text("utf-8"))["expectations"]}

    # -- fixture -> in-memory rows
    def build(self, sid, building_code=None):
        s = dict(self.scenarios[sid])
        if "reuses_data_of" in s:                                     # S-R carries only a request; its data is S-M's
            donor = self.scenarios[s["reuses_data_of"]]
            s.update({k: donor[k] for k in ("assets", "source", "core_ingestion_batches", "raw_vendor_events")})
        src_of = {a["asset_code"]: a.get("source_code") or s["source"]["source_code"] for a in s["assets"]}
        types = {"camera_offline": "FAULT_OPEN", "link_down": "FAULT_OPEN", "camera_online": "FAULT_CLOSE", "link_up": "FAULT_CLOSE"}
        asset_ids, source_ids, event_ids, rows = {}, {}, {}, []
        sys_of = {a["asset_code"]: a["system_type"] for a in s["assets"]}
        ext_of = {a["asset_code"]: a["external_id"] for a in s["assets"]}
        bld_of = {a["asset_code"]: a.get("building_code") for a in s["assets"]}

        def add(fixture_id, code, etype, ts, identity, source_code, with_source=True):
            if etype not in types or (building_code and bld_of.get(code) not in (None, building_code)):
                return
            aid = asset_ids.setdefault(code, len(asset_ids) + 1)
            sid_ = source_ids.setdefault(source_code, len(source_ids) + 1) if with_source else None
            eid = len(event_ids) + 1
            event_ids[(fixture_id, eid)] = eid
            event_ids.setdefault(fixture_id, eid)                    # first delivery wins for the id mapping
            rows.append({"event_id": eid, "ts": u(ts), "event_type": etype, "event_class": types[etype], "source_id": sid_,
                         "source_code": source_code if with_source else None, "source_event_id": identity, "asset_id": aid,
                         "asset_code": code, "asset_name": code, "system_code": sys_of[code], "external_ids": [ext_of[code]],
                         "_fixture_id": fixture_id})

        for batch in s["core_ingestion_batches"]:
            for e in batch["events"]:
                add(e["source_event_id"], e["asset_code"], e["event_type"], e["observed_at"], e["source_event_id"], src_of[e["asset_code"]])
        for e in s.get("legacy_stored_rows", []):
            add(e["fixture_row_id"], e["asset_code"], e["event_type"], e["observed_at"], None, src_of[e["asset_code"]], with_source=False)
        return s, rows, {k: v for k, v in event_ids.items() if isinstance(k, str)}

    def provider(self, s):
        cov = s["source_coverage"]
        if cov["status"] == "NOT_APPLICABLE_CAPABILITY_UNSUPPORTED":
            return lambda b, st, en, codes: {c: SourceCoverage(c, False, False, None, None, None) for c in codes}
        done = cov["status"] == "FIXTURE_ASSERTED_COMPLETE"
        a, b_, asof = (u(cov[k]) if cov.get(k) else None for k in ("covered_from_utc", "covered_to_utc", "data_as_of"))
        return lambda b, st, en, codes: {c: SourceCoverage(c, True, done, a, b_, asof) for c in codes}

    def replay(self, sid, building_code=None, **overrides):
        s, rows, ids = self.build(sid, building_code)
        bcode = building_code or s["building"]["building_code"]
        sources = sorted({r["source_code"] for r in rows if r["source_code"]}) or [s["source"]["source_code"]]
        request = dict(s["request"])
        args = {k: request[k] for k in ("start", "end") if k in request}
        if request.get("systems"):
            args["systems"] = request["systems"]
        args.update(overrides)
        building = {"building_id": 1, "building_code": bcode, "timezone": s["building"]["timezone"]}
        cur = FakeCursor(building, rows, sources)
        env = dof.run_devices_offline_frequency(cur, building=bcode, coverage_provider=self.provider(s), clock=lambda: NOW, **args)
        return env.to_dict(), ids, s

    def check_row(self, got, exp, ids):
        inv = {str(v): k for k, v in ids.items()}
        refs = lambda lst: [inv[x["id"]] for x in lst]
        for key in ("source_code", "building_code"):
            if key in exp and "(" not in str(exp[key]):
                self.assertEqual(got[key], exp[key], key)
        self.assertEqual(got["asset_external_id"], exp["asset_external_id"])
        for key in ("initial_state", "outages_started_in_period", "outages_ongoing_at_period_start", "downtime_seconds_in_period",
                    "open_at_period_end", "duration_is_lower_bound", "flapping", "orphan_offline_end_count"):
            self.assertEqual(got[key], exp[key], f"{exp['asset_external_id']}.{key}")
        if "state_at_period_end" in exp:
            self.assertEqual(got["state_at_period_end"], exp["state_at_period_end"], f"{exp['asset_external_id']}.state_at_period_end")
        self.assertEqual(len(got["sessions"]), len(exp["sessions"]))
        for gs, es in zip(got["sessions"], exp["sessions"]):
            self.assertEqual(gs["start_utc"], es["start_utc"])
            self.assertEqual(gs["end_utc"], es["end_utc"])
            self.assertEqual(gs["overlap_seconds"], es["overlap_seconds"])
            for key in ("started_in_period", "ongoing_at_period_start", "overlap_start_utc", "overlap_end_utc", "cutoff_utc", "recovery_observed"):
                if key in es:
                    self.assertEqual(gs[key], es[key], key)
            self.assertEqual(refs(gs["evidence_refs"]), es["evidence_refs"])
        if "orphan_offline_end_refs" in exp:
            self.assertEqual(refs(got["orphan_offline_end_refs"]), exp["orphan_offline_end_refs"])
        return refs(got["evidence_refs"])

    def check(self, sid, *, status=None, complete=None):
        d, ids, s = self.replay(sid)
        exp = self.expect[sid]["expected"]
        self.assertEqual(d["status"], status or exp["result_status_proposed"])
        self.assertEqual(d["complete"], exp["complete"] if complete is None else complete)
        self.assertEqual((d["total_rows"], d["truncated"]), (exp["total_rows"], exp["truncated"]))
        self.assertEqual(len(d["rows"]), len(exp["rows"]))
        for got, want in zip(d["rows"], exp["rows"]):
            self.check_row(got, want, ids)
            self.assertEqual(sorted(want["evidence_refs"]), sorted(self.evidence_of(got, ids)), sid)
        want_codes = {self.LIMITATION_LABELS.get(c, c) for c in exp["limitations"]}
        return d, ids, want_codes

    @staticmethod
    def core(d):
        """Limitation codes minus DATA_AS_OF_UNKNOWN, which the envelope adds whenever data_as_of is unconfirmed and the
        fixture oracle never lists."""
        return {c for c in codes(d) if c != "DATA_AS_OF_UNKNOWN"}

    def evidence_of(self, got, ids):
        inv = {str(v): k for k, v in ids.items()}
        return [inv[x["id"]] for x in got["evidence_refs"]]

    # -- scenarios whose oracle this implementation matches exactly
    def test_C1_duplicate_delivery(self):                                                # G-DUP-01
        d, ids, want = self.check("S-C1-DUPLICATE-DELIVERY")
        self.assertEqual(row(d)["duplicate_events_ignored"], self.expect["S-C1-DUPLICATE-DELIVERY"]["expected"]["duplicate_deliveries_ignored"])
        self.assertEqual(self.core(d), want)

    def test_C2_legacy_null_identity_is_never_merged(self):                              # G-DUP-02
        d, ids, want = self.check("S-C2-LEGACY-NULL-IDENTITY")
        self.assertEqual(self.core(d), want)
        self.assertEqual(len(row(d)["sessions"][0]["evidence_refs"]), 3)

    def test_F_repeated_offline(self):                                                   # G-OFF-03
        d, ids, want = self.check("S-F-REPEATED-OFFLINE")
        self.assertEqual(self.core(d), want)

    def test_G1_outage_started_before_period(self):                                      # G-OFF-08
        d, ids, want = self.check("S-G1-OUTAGE-STARTED-BEFORE-PERIOD")
        self.assertEqual(self.core(d), want)

    def test_G2_no_earlier_transition(self):                                             # G-OFF-09
        d, ids, want = self.check("S-G2-NO-EARLIER-TRANSITION")
        self.assertEqual(self.core(d), want)

    def test_H2_open_outage_data_as_of_after_end(self):                                  # G-OFF-05 / G-OFF-11
        d, ids, want = self.check("S-H2-OPEN-OUTAGE-DATA-AS-OF-AFTER-END")
        self.assertEqual(row(d)["sessions"][0]["cutoff_reason"], "period_end")           # fixture: "period end is earlier than data_as_of"
        self.assertEqual(self.core(d), want)

    def test_K_flapping_disabled(self):                                                  # G-OFF-06 / G-OFF-07
        d, ids, want = self.check("S-K-FLAPPING-DISABLED")
        self.assertEqual((row(d)["outages_started_in_period"], row(d)["flapping"]), (3, "not_evaluated"))
        self.assertEqual(self.core(d), want)

    def test_L_fault_pairs_are_not_offline(self):                                        # G-OFF-01
        d, ids, want = self.check("S-L-FAULT-PAIRS-NOT-OFFLINE")
        self.assertEqual(d["limitations"][0]["details"]["unsupported_systems"], sorted(self.expect["S-L-FAULT-PAIRS-NOT-OFFLINE"]["expected"]["unsupported_systems"]))
        self.assertEqual(self.core(d), want)

    def test_J_source_silence_is_no_outage(self):                                        # G-OFF-02
        d, ids, want = self.check("S-J-SOURCE-SILENCE")
        self.assertEqual((d["rows"], d["status"]), ([], "PARTIAL"))
        self.assertEqual(self.core(d), want)
        # fixture also lists uncovered_seconds_in_period=57600; the envelope has no field for it (documented)

    def test_O1_page_failure_is_partial_with_partial_rows(self):                         # G-COMP-01 / G-COMP-04
        d, ids, want = self.check("S-O1-PAGINATION-FAILURE")
        self.assertTrue(want <= self.core(d))
        self.assertEqual(row(d)["outages_started_in_period"], 2)

    def test_O2_capability_unsupported(self):                                            # G-COMP-02
        d, ids, want = self.check("S-O2-CAPABILITY-UNSUPPORTED")
        self.assertEqual(self.core(d), want)

    def test_O3_complete_zero(self):                                                     # G-COMP-03
        d, ids, want = self.check("S-O3-COMPLETE-ZERO-RESULT")
        self.assertEqual((d["status"], d["complete"], d["total_rows"], d["limitations"]), ("COMPLETE", True, 0, []))

    # -- H1: coverage ends before the period end (ADR D-09 / D-12, fixture 0.3 oracle)
    def test_H1_coverage_ends_before_period_end_is_partial_and_end_state_unknown(self):  # G-OFF-10 / G-OFF-12
        d, ids, s = self.replay("S-H1-OPEN-OUTAGE-DATA-AS-OF-BEFORE-END")
        exp = self.expect["S-H1-OPEN-OUTAGE-DATA-AS-OF-BEFORE-END"]["expected"]
        self.check_row(row(d), exp["rows"][0], ids)                                      # incl. state_at_period_end = UNKNOWN
        self.assertEqual(row(d)["sessions"][0]["cutoff_reason"], "data_as_of")           # open only up to the last trusted observation
        self.assertEqual((d["status"], d["complete"]), (exp["result_status_proposed"], exp["complete"]))
        self.assertEqual((d["status"], d["complete"], row(d)["state_at_period_end"]), ("PARTIAL", False, "UNKNOWN"))
        self.assertIn("COMPLETION_NOT_CONFIRMED", codes(d))
        self.assertEqual(self.core(d), {self.LIMITATION_LABELS.get(c, c) for c in exp["limitations"]})

    def test_I_orphan_end_bounded_lookback_evidence(self):                               # G-OFF-04
        d, ids, s = self.replay("S-I-ORPHAN-OFFLINE-END")
        exp = self.expect["S-I-ORPHAN-OFFLINE-END"]["expected"]
        r, want = row(d), exp["rows"][0]
        for key in ("initial_state", "outages_started_in_period", "downtime_seconds_in_period", "orphan_offline_end_count", "sessions"):
            self.assertEqual(r[key], want[key], key)
        self.assertEqual(self.evidence_of(r, ids), ["EV-I-002", "EV-I-003"])           # fixture lists EV-I-001 too: the look-back keeps
        self.assertEqual(want["evidence_refs"], ["EV-I-001", "EV-I-002", "EV-I-003"])  # only the recovery that proves ONLINE
        self.assertEqual(self.core(d), {self.LIMITATION_LABELS.get(c, c) for c in exp["limitations"]})

    def test_M_ranking_ties_per_building(self):                                          # fixture ranks across two buildings
        exp = self.expect["S-M-RANKING-TIES"]["expected"]["rows"]
        order = []
        for bld in ("BLD-BRAVO", "BLD-ALPHA"):
            d, ids, s = self.replay("S-M-RANKING-TIES", building_code=bld)
            order += [(bld, r["asset_external_id"], r["outages_started_in_period"], r["downtime_seconds_in_period"]) for r in d["rows"]]
            self.assertEqual(d["status"], "COMPLETE")
        by_building = {b: [e for e in exp if e["building_code"] == b] for b in ("BLD-BRAVO", "BLD-ALPHA")}
        for bld in ("BLD-BRAVO", "BLD-ALPHA"):
            got = [o[1:] for o in order if o[0] == bld]
            self.assertEqual(got, [(e["asset_external_id"], e["outages_started_in_period"], e["downtime_seconds_in_period"]) for e in by_building[bld]])

    def test_N_untrusted_and_missing_timestamps_are_not_representable(self):            # G-TS-01..03
        d, ids, s = self.replay("S-N-MISSING-UNTRUSTED-TIMESTAMPS")
        exp = self.expect["S-N-MISSING-UNTRUSTED-TIMESTAMPS"]["expected"]
        self.assertEqual((row(d)["downtime_seconds_in_period"], row(d)["outages_started_in_period"]), (600, 1))   # the GOOD events agree
        self.assertEqual(exp["result_status_proposed"], "PARTIAL")                       # expected only because of the quarantined records,
        self.assertEqual(d["status"], "COMPLETE")                                        # which contract 1.0 cannot store or flag

    # -- scenarios without an expectations entry: asserted from ADR rules / tzdata
    def test_P_timezone_missing_or_invalid(self):                                        # G-TZ-01
        for sid, bld in (("S-P1-TIMEZONE-MISSING", "BLD-NOTZ"), ("S-P2-TIMEZONE-INVALID", "BLD-BADTZ")):
            d, ids, s = self.replay(sid)
            self.assertEqual((d["status"], d["rows"], d["limitations"][0]["code"]), ("CANNOT_COMPUTE", [], "TIMEZONE_UNAVAILABLE"), sid)

    def test_Q1_timestamp_without_offset_rejected(self):                                 # G-TZ-02
        d, ids, s = self.replay("S-Q1-TIMESTAMP-WITHOUT-OFFSET")
        self.assertEqual((d["status"], d["limitations"][0]["code"]), ("CANNOT_COMPUTE", "INVALID_PARAMETER"))

    def test_Q2_timestamp_with_offset(self):                                             # G-PER-01
        d, ids, s = self.replay("S-Q2-TIMESTAMP-WITH-OFFSET")
        r = row(d)
        self.assertEqual((d["status"], r["outages_started_in_period"], r["downtime_seconds_in_period"], r["open_at_period_end"]), ("COMPLETE", 1, 600, False))
        self.assertEqual(d["parameters"]["start_utc"], "2026-11-01T22:00:00Z")

    def test_R_truncation(self):
        d, ids, s = self.replay("S-R-TRUNCATION", building_code="BLD-BRAVO")
        self.assertEqual(d["total_rows"], 4)
        s_, rows, _ = self.build("S-R-TRUNCATION", "BLD-BRAVO")
        cur = FakeCursor({"building_id": 1, "building_code": "BLD-BRAVO", "timezone": "Africa/Cairo"}, rows, ["SRC-M-1", "SRC-M-2"])
        t = dof.run_devices_offline_frequency(cur, building="BLD-BRAVO", start="2026-11-02", end="2026-11-02", coverage_provider=self.provider(s_),
                                              max_rows=s_["request"]["fixture_only_max_rows"], clock=lambda: NOW).to_dict()
        self.assertEqual((len(t["rows"]), t["total_rows"], t["truncated"], t["complete"]), (3, 4, True, False))
        self.assertEqual([r["asset_external_id"] for r in t["rows"]], ["EXT-SW-M-1", "EXT-CAM-M-1", "EXT-CAM-M-2"])

    def test_T1_dst_missing_midnight(self):                                              # G-PER-03 (hand-derived from tzdata)
        d, ids, s = self.replay("S-T1-DST-MIDNIGHT-MISSING")
        r = row(d)
        self.assertEqual((d["parameters"]["start_utc"], d["parameters"]["end_utc_exclusive"]), ("2026-03-08T05:00:00Z", "2026-03-09T04:00:00Z"))
        # EV-T1-001 04:00Z is before the period (ongoing at start); recovery 06:00Z: 1 h overlap.
        # EV-T1-003 03:30Z next day starts in the period; its recovery (05:00Z) is after the period end: open, 30 min.
        self.assertEqual((r["outages_started_in_period"], r["outages_ongoing_at_period_start"], r["downtime_seconds_in_period"], r["open_at_period_end"]),
                         (1, 1, 5400, True))

    @unittest.skipUnless(HOST_TZDB_HAS_CUBA_2026F, SKIP_OLD_TZDB)
    def test_T2_havana_cancelled_fallback_2026f(self):                                   # S-T2 fixture, tzdb 2026f
        d, ids, s = self.replay("S-T2-DST-MIDNIGHT-REPEATED")
        r = row(d)
        self.assertEqual((d["parameters"]["start_utc"], d["parameters"]["end_utc_exclusive"]), ("2026-11-01T04:00:00Z", "2026-11-02T04:00:00Z"))
        self.assertEqual((r["outages_started_in_period"], r["outages_ongoing_at_period_start"], r["downtime_seconds_in_period"], r["open_at_period_end"]),
                         (1, 0, 3600, False))


class PeriodEndStateTests(unittest.TestCase):
    """H1 (ADR D-09/D-12): the device state at the requested period end is known only when trusted coverage reaches it."""

    OFF_20 = [ev(1, "2026-11-02T20:00:00Z", "off")]

    def test_open_outage_with_coverage_through_the_end_is_offline_at_period_end(self):
        r = row(run(self.OFF_20, covered()))
        self.assertEqual((r["state_at_period_end"], r["open_at_period_end"]), (OFFLINE, True))

    def test_recovered_outage_with_coverage_through_the_end_is_online_at_period_end(self):
        r = row(run([ev(1, "2026-11-02T10:00:00Z", "off"), ev(2, "2026-11-02T11:00:00Z", "on")], covered()))
        self.assertEqual((r["state_at_period_end"], r["open_at_period_end"]), (ONLINE, False))

    def test_coverage_ending_before_the_period_end_makes_the_end_state_unknown(self):
        d = run(self.OFF_20, covered(data_as_of=u("2026-11-02T21:00:00Z"), covered_to=u("2026-11-02T21:00:00Z")))
        self.assertEqual((d["status"], d["complete"], row(d)["state_at_period_end"]), ("PARTIAL", False, UNKNOWN))
        self.assertEqual(row(d)["sessions"][0]["cutoff_reason"], "data_as_of")

    def test_recovered_outage_is_also_unknown_when_coverage_stops_early(self):
        rows = [ev(1, "2026-11-02T10:00:00Z", "off"), ev(2, "2026-11-02T11:00:00Z", "on")]
        d = run(rows, covered(data_as_of=u("2026-11-02T21:00:00Z"), covered_to=u("2026-11-02T21:00:00Z")))
        self.assertEqual(row(d)["state_at_period_end"], UNKNOWN)      # a later offline event may simply not be retrieved yet

    def test_confirmed_coverage_but_data_as_of_before_the_end_stays_unknown(self):
        d = run(self.OFF_20, covered(data_as_of=u("2026-11-02T21:00:00Z")))
        self.assertEqual((row(d)["sessions"][0]["cutoff_reason"], row(d)["state_at_period_end"]), ("data_as_of", UNKNOWN))

    def test_no_coverage_provider_means_unknown_never_a_claim_from_query_time(self):
        d = run(self.OFF_20)                                          # NOW is long after the period, but that proves nothing
        self.assertEqual((d["status"], row(d)["state_at_period_end"]), ("PARTIAL", UNKNOWN))

    def test_period_not_yet_ended_is_unknown(self):
        d = dof.run_devices_offline_frequency(FakeCursor(CAIRO, self.OFF_20, ("SRC-1",)), building="BLD-A", coverage_provider=covered(),
                                              clock=lambda: u("2026-11-02T21:00:00Z"), **DAY).to_dict()
        self.assertEqual((row(d)["sessions"][0]["cutoff_reason"], row(d)["state_at_period_end"]), ("query_time", UNKNOWN))

    def test_no_evidence_at_all_stays_unknown_even_with_full_coverage(self):
        # started before the period, never any event: no row is emitted, so nothing is claimed
        self.assertEqual(run([], covered())["rows"], [])


class RankingMetricTests(unittest.TestCase):
    """The default ranking metric is outages_started_in_period (fixture 0.2 'team confirmation'; ADR D-13)."""

    ROWS = [ev(1, "2026-11-01T10:00:00Z", "off", asset=1),                                       # down since before the period, never recovers
            ev(2, "2026-11-02T10:00:00Z", "off", asset=2), ev(3, "2026-11-02T10:05:00Z", "on", asset=2)]  # one short outage in the period

    def test_default_rank_counts_sessions_started_in_the_period_and_ignores_ongoing_ones(self):
        d = run(self.ROWS, covered())
        self.assertEqual([(r["asset_external_id"], r["outages_started_in_period"], r["outages_ongoing_at_period_start"], r["downtime_seconds_in_period"])
                          for r in d["rows"]], [("EXT-2", 1, 0, 300), ("EXT-1", 0, 1, 86400)])
        self.assertEqual(d["parameters"]["rank_by"], "outages_started")

    def test_downtime_is_a_separate_explicit_metric_never_a_silent_substitute(self):
        d = run(self.ROWS, covered(), rank_by="downtime_seconds")
        self.assertEqual([r["asset_external_id"] for r in d["rows"]], ["EXT-1", "EXT-2"])
        self.assertEqual(d["parameters"]["rank_by"], "downtime_seconds")

    def test_three_outage_notions_are_separate_columns(self):
        r = {x["asset_external_id"]: x for x in run(self.ROWS, covered())["rows"]}
        self.assertEqual((r["EXT-1"]["outages_started_in_period"], r["EXT-1"]["outages_ongoing_at_period_start"], r["EXT-1"]["open_at_period_end"]), (0, 1, True))
        self.assertEqual((r["EXT-2"]["outages_started_in_period"], r["EXT-2"]["outages_ongoing_at_period_start"], r["EXT-2"]["open_at_period_end"]), (1, 0, False))


class SyntheticSpringForward(tzinfo):
    """Local 00:00-00:59 on 2026-03-08 does not exist (UTC-5 -> UTC-4 at 05:00Z). No tzdb involved."""
    key = "TaskTest/Synthetic-Spring-Forward"

    def utcoffset(self, value):
        day = (value.year, value.month, value.day)
        if day < (2026, 3, 8):
            return timedelta(hours=-5)
        if day == (2026, 3, 8) and value.hour == 0:
            return timedelta(hours=-5 if value.fold == 0 else -4)
        return timedelta(hours=-4)

    def dst(self, value):
        return timedelta(0)

    def tzname(self, value):
        return "SYNTHETIC"


class SyntheticFallBack(tzinfo):
    """Local 00:00-00:59 on 2026-11-01 happens twice (UTC-4 first, then UTC-5). No tzdb involved."""
    key = "TaskTest/Synthetic-Fall-Back"

    def utcoffset(self, value):
        day = (value.year, value.month, value.day)
        if day < (2026, 11, 1):
            return timedelta(hours=-4)
        if day == (2026, 11, 1) and value.hour == 0:
            return timedelta(hours=-4 if value.fold == 0 else -5)
        return timedelta(hours=-5)

    def dst(self, value):
        return timedelta(0)

    def tzname(self, value):
        return "SYNTHETIC"


class SyntheticTimezoneTests(unittest.TestCase):
    """ADR D-10 regression tests that do not depend on the host's installed IANA database."""

    def test_missing_midnight_uses_the_first_valid_instant_and_the_day_is_23_hours(self):
        zone = SyntheticSpringForward()
        start = resolve_local(date(2026, 3, 8), time(0, 0), zone, midnight_rule=True)
        end = resolve_local(date(2026, 3, 9), time(0, 0), zone, midnight_rule=True)
        self.assertEqual((start, end), (u("2026-03-08T05:00:00Z"), u("2026-03-09T04:00:00Z")))
        self.assertEqual(end - start, timedelta(hours=23))

    def test_any_other_nonexistent_local_time_is_an_error_not_a_guess(self):
        with self.assertRaises(PeriodError) as ctx:
            resolve_local(date(2026, 3, 8), time(0, 30), SyntheticSpringForward(), midnight_rule=False)
        self.assertEqual(ctx.exception.code, "INVALID_PARAMETER")

    def test_repeated_midnight_uses_the_first_occurrence_and_the_day_is_25_hours(self):
        zone = SyntheticFallBack()
        start = resolve_local(date(2026, 11, 1), time(0, 0), zone, midnight_rule=True)
        end = resolve_local(date(2026, 11, 2), time(0, 0), zone, midnight_rule=True)
        self.assertEqual((start, end), (u("2026-11-01T04:00:00Z"), u("2026-11-02T05:00:00Z")))
        self.assertEqual(end - start, timedelta(hours=25))

    def test_any_other_ambiguous_local_time_is_an_error_not_a_guess(self):
        with self.assertRaises(PeriodError):
            resolve_local(date(2026, 11, 1), time(0, 30), SyntheticFallBack(), midnight_rule=False)

    def test_cancelled_fallback_is_a_plain_24_hour_day(self):             # the 2026f Havana rule, as a fixed UTC-4 offset
        zone = timezone(timedelta(hours=-4))
        start = resolve_local(date(2026, 11, 1), time(0, 0), zone, midnight_rule=True)
        end = resolve_local(date(2026, 11, 2), time(0, 0), zone, midnight_rule=True)
        self.assertEqual((start, end - start), (u("2026-11-01T04:00:00Z"), timedelta(hours=24)))

    def test_adjacent_periods_share_a_boundary_and_never_overlap(self):   # half-open [start, end)
        zone = SyntheticFallBack()
        day1_end = resolve_local(date(2026, 11, 2), time(0, 0), zone, midnight_rule=True)
        day2_start = resolve_local(date(2026, 11, 2), time(0, 0), zone, midnight_rule=True)
        self.assertEqual(day1_end, day2_start)


if __name__ == "__main__":
    unittest.main()
