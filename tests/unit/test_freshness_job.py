from __future__ import annotations

import json
import logging
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from scripts import freshness_job


class EmptyCursor:
    rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, *_args):
        self.statement = _args[0]

    def fetchall(self):
        return []

    def fetchone(self):
        if "pg_try_advisory_lock" in self.statement:
            return {"locked": True}
        if "pg_advisory_unlock" in self.statement:
            return {"unlocked": True}
        return None


class EmptyConnection:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return EmptyCursor()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class FreshnessJobTests(unittest.TestCase):
    def test_database_environment_requires_explicit_host_port_and_database(self):
        with self.assertRaisesRegex(ValueError, "PGHOST, PGPORT"):
            freshness_job.connect_from_env({"PGDATABASE": "anzma", "PGUSER": "test"})
        with self.assertRaisesRegex(ValueError, "PGPORT must be"):
            freshness_job.connect_from_env({"PGHOST": "127.0.0.1", "PGPORT": "0",
                                            "PGDATABASE": "anzma_e3_test", "PGUSER": "test"})

    def test_loop_uses_injected_clock_and_sleeper_without_sleeping(self):
        conn = EmptyConnection()
        ticks = iter((datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
                      datetime(2026, 11, 20, 12, 1, tzinfo=timezone.utc)))
        delays = []
        lines = []
        result = freshness_job.run_loop(conn, interval=60, clock=lambda: next(ticks),
                                        sleeper=delays.append, max_passes=2, emit=lines.append)
        self.assertEqual(result, 2)
        self.assertEqual(delays, [60])
        self.assertGreaterEqual(conn.commits, 2)
        self.assertEqual([json.loads(line)["evaluated_at"] for line in lines],
                         ["2026-11-20T12:00:00Z", "2026-11-20T12:01:00Z"])

    def test_loop_finishes_current_pass_then_stops_gracefully(self):
        conn = EmptyConnection()
        stop_event = threading.Event()
        delays = []

        def request_stop(delay):
            delays.append(delay)
            stop_event.set()

        with patch.object(freshness_job, "run_pass", return_value={"event": "FRESHNESS_PASS"}) as run_pass:
            result = freshness_job.run_loop(conn, interval=23, max_passes=None, sleeper=request_stop,
                                            stop_event=stop_event, emit=lambda _line: None)
        self.assertEqual(result, 1)
        self.assertEqual(delays, [23])
        self.assertEqual(run_pass.call_count, 1)

    def test_instance_lock_waits_then_acquires_and_releases(self):
        class LockCursor(EmptyCursor):
            def fetchone(self):
                if "pg_try_advisory_lock" in self.statement:
                    return {"locked": next(self.lock_results)}
                if "pg_advisory_unlock" in self.statement:
                    return {"unlocked": True}

        class LockConnection(EmptyConnection):
            def __init__(self):
                super().__init__()
                self.lock_results = iter((False, True))

            def cursor(self):
                cursor = LockCursor()
                cursor.lock_results = self.lock_results
                return cursor

        conn = LockConnection()
        stop_event = threading.Event()
        waits, lines = [], []
        acquired = freshness_job.acquire_instance_lock(
            conn, stop_event, retry_seconds=5, emit=lines.append,
            waiter=lambda delay: waits.append(delay) or False,
        )
        self.assertTrue(acquired)
        self.assertEqual(waits, [5])
        self.assertEqual([json.loads(line)["event"] for line in lines],
                         ["FRESHNESS_LOCK_WAIT", "FRESHNESS_LOCK_ACQUIRED"])
        freshness_job.release_instance_lock(conn, emit=lines.append)
        self.assertEqual(json.loads(lines[-1])["event"], "FRESHNESS_LOCK_RELEASED")

    def test_instance_lock_wait_is_interruptible(self):
        class NeverLockCursor(EmptyCursor):
            def fetchone(self):
                return {"locked": False}

        class NeverLockConnection(EmptyConnection):
            def cursor(self):
                return NeverLockCursor()

        conn = NeverLockConnection()
        stop_event = threading.Event()

        def cancel(_delay):
            stop_event.set()
            return True

        self.assertFalse(freshness_job.acquire_instance_lock(
            conn, stop_event, emit=lambda _line: None, waiter=cancel))
        self.assertEqual(conn.commits, 1)

    def test_invalid_interval_environment_uses_default_and_warns(self):
        freshness_job._WARNED_SETTINGS.discard("ANZMA_FRESHNESS_INTERVAL_SECONDS")
        with self.assertLogs(freshness_job.LOGGER, level=logging.WARNING) as captured:
            interval = freshness_job._positive_setting(
                {"ANZMA_FRESHNESS_INTERVAL_SECONDS": "0"},
                "ANZMA_FRESHNESS_INTERVAL_SECONDS", 60)
        self.assertEqual(interval, 60)
        self.assertEqual(len(captured.output), 1)

    def test_main_uses_environment_interval_and_releases_lock_on_stop(self):
        conn = EmptyConnection()
        lines = []
        with patch.object(freshness_job, "run_loop", return_value=1) as run_loop:
            code = freshness_job.main(
                ["--loop"], env={"ANZMA_FRESHNESS_INTERVAL_SECONDS": "17"},
                connect_fn=lambda _env: conn, emit=lines.append)
        self.assertEqual(code, 0)
        self.assertEqual(run_loop.call_args.kwargs["interval"], 17)
        self.assertEqual(json.loads(lines[0])["event"], "FRESHNESS_LOCK_ACQUIRED")
        self.assertEqual(json.loads(lines[-1])["event"], "FRESHNESS_LOCK_RELEASED")

    def test_failed_pass_emits_failure_json_returns_nonzero_and_closes_connection(self):
        conn = EmptyConnection()
        lines = []
        with patch.object(freshness_job, "run_pass", side_effect=RuntimeError("query failed")):
            code = freshness_job.main(["--once"], env={}, connect_fn=lambda _env: conn, emit=lines.append)
        failures = [json.loads(line) for line in lines if json.loads(line)["event"] == "FRESHNESS_PASS_FAILED"]
        self.assertEqual(code, 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["error"], "RuntimeError: query failed")
        self.assertTrue(conn.closed)
        self.assertEqual(json.loads(lines[-1])["event"], "FRESHNESS_LOCK_RELEASED")

    def test_once_emits_summary_and_closes_connection(self):
        conn = EmptyConnection()
        lines = []
        now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        code = freshness_job.main(["--once"], env={"PGHOST": "127.0.0.1", "PGPORT": "25443"},
                                  connect_fn=lambda _env: conn, clock=lambda: now, emit=lines.append)
        self.assertEqual(code, 0)
        self.assertTrue(conn.closed)
        summary = next(json.loads(line) for line in lines if json.loads(line)["event"] == "FRESHNESS_PASS")
        self.assertEqual(summary["event"], "FRESHNESS_PASS")
        self.assertEqual(summary["series_evaluated"], 0)
        self.assertEqual(summary["states"]["NOT_APPLICABLE"], 0)

    def test_asset_state_failure_does_not_fail_freshness_pass(self):
        conn = EmptyConnection()
        now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
        freshness_result = {
            "evaluated_at": now, "series_evaluated": 2,
            "states": {"FRESH": 2, "STALE": 0, "MISSING": 0, "UNKNOWN": 0,
                       "NOT_APPLICABLE": 0},
            "gaps_opened": 0, "gaps_closed": 0, "sources_evaluated": 0,
            "source_states": {"ALIVE": 0, "SILENT": 0, "UNKNOWN": 0, "DISABLED": 0},
            "gaps_reclassified": 0, "pages_committed": 1,
        }
        with patch.object(freshness_job, "run_once", return_value=freshness_result), \
             patch.object(freshness_job.asset_state_service, "run_once",
                          side_effect=RuntimeError("shadow store unavailable")), \
             self.assertLogs(freshness_job.LOGGER, level=logging.ERROR) as logs:
            summary = freshness_job.run_pass(conn, clock=lambda: now)
        self.assertEqual(summary["event"], "FRESHNESS_PASS")
        self.assertEqual(summary["series_evaluated"], 2)
        self.assertEqual(conn.rollbacks, 1)
        failure_json = logs.output[0].split(":", 2)[-1]
        failure = json.loads(failure_json)
        self.assertEqual(failure["event"], "ASSET_STATE_PASS_FAILED")
        self.assertIn("shadow store unavailable", failure["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
