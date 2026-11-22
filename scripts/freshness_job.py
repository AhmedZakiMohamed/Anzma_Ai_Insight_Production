#!/usr/bin/env python3
"""Run one freshness evaluation pass or repeat it on a fixed interval.

Set PGHOST, PGPORT, PGDATABASE, and PGUSER explicitly. There are no database
host/port defaults so an accidental connection to a shared local stack is avoided.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.adapters.database import db_adapter as da
from app.services.freshness import MAX_ERROR_CHARS, run_once
from app.services import asset_state as asset_state_service

LOGGER = logging.getLogger("anzma.freshness.job")
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_LOCK_RETRY_SECONDS = 5
INSTANCE_LOCK_ID = 718230643
_WARNED_SETTINGS: set[str] = set()


def connect_from_env(env: Mapping[str, str] | None = None) -> Any:
    settings = os.environ if env is None else env
    missing = [name for name in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER") if not settings.get(name)]
    if missing:
        raise ValueError("explicit PostgreSQL settings are required: " + ", ".join(missing))
    try:
        port = int(settings["PGPORT"])
    except ValueError as exc:
        raise ValueError("PGPORT must be an integer between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError("PGPORT must be an integer between 1 and 65535")
    return da.connect(
        host=settings["PGHOST"], port=port, dbname=settings["PGDATABASE"],
        user=settings["PGUSER"], password=settings.get("PGPASSWORD", ""),
    )


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def run_pass(
    conn: Any,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    started = monotonic()
    now = clock()
    result = run_once(conn, now)
    try:
        # run_once commits freshness before the shadow stage begins. The shadow
        # evaluator owns its transaction; its failure cannot roll back freshness.
        asset_state_service.run_once(conn, result["evaluated_at"])
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        LOGGER.error(json.dumps({
            "event": "ASSET_STATE_PASS_FAILED",
            "evaluated_at": _iso_utc(result["evaluated_at"]),
            "error": f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS],
        }, separators=(",", ":")))
    duration = max(0.0, monotonic() - started)
    return {
        "event": "FRESHNESS_PASS",
        "evaluated_at": _iso_utc(result["evaluated_at"]),
        "series_evaluated": result["series_evaluated"],
        "states": result["states"],
        "gaps_opened": result["gaps_opened"],
        "gaps_closed": result["gaps_closed"],
        "sources_evaluated": result["sources_evaluated"],
        "source_states": result["source_states"],
        "gaps_reclassified": result["gaps_reclassified"],
        "pages_committed": result["pages_committed"],
        "duration_s": round(duration, 3),
    }


def run_loop(
    conn: Any,
    interval: int = 60,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    max_passes: int | None = None,
    emit: Callable[[str], None] = print,
    stop_event: threading.Event | None = None,
) -> int:
    if interval < 1:
        raise ValueError("interval must be at least one second")
    completed = 0
    while max_passes is None or completed < max_passes:
        if stop_event is not None and stop_event.is_set():
            break
        summary = run_pass(conn, clock=clock, monotonic=monotonic)
        emit(json.dumps(summary, separators=(",", ":")))
        completed += 1
        if max_passes is not None and completed >= max_passes:
            break
        if stop_event is None:
            sleeper(interval)
        elif sleeper is not time.sleep:
            sleeper(interval)
        elif stop_event.wait(interval):
            break
    return completed


def _positive_setting(settings: Mapping[str, str], name: str, default: int) -> int:
    raw = settings.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    if name not in _WARNED_SETTINGS:
        LOGGER.warning("Invalid %s; using safe default %d", name, default)
        _WARNED_SETTINGS.add(name)
    return default


def acquire_instance_lock(
    conn: Any,
    stop_event: threading.Event,
    retry_seconds: int = DEFAULT_LOCK_RETRY_SECONDS,
    emit: Callable[[str], None] = print,
    waiter: Callable[[float], bool] | None = None,
) -> bool:
    """Hold a PostgreSQL session advisory lock for this process's full lifetime."""
    if retry_seconds < 1:
        raise ValueError("lock retry interval must be at least one second")
    waiting_reported = False
    while not stop_event.is_set():
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s) AS locked", (INSTANCE_LOCK_ID,))
            row = cur.fetchone()
        conn.commit()
        locked = row["locked"] if isinstance(row, Mapping) else row[0]
        if locked:
            emit(json.dumps({"event": "FRESHNESS_LOCK_ACQUIRED"}, separators=(",", ":")))
            return True
        if not waiting_reported:
            emit(json.dumps({"event": "FRESHNESS_LOCK_WAIT"}, separators=(",", ":")))
            waiting_reported = True
        wait = waiter or stop_event.wait
        if wait(retry_seconds):
            break
    return False


def release_instance_lock(conn: Any, emit: Callable[[str], None] = print) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s) AS unlocked", (INSTANCE_LOCK_ID,))
            row = cur.fetchone()
        conn.commit()
        unlocked = row["unlocked"] if isinstance(row, Mapping) else row[0]
        if unlocked:
            emit(json.dumps({"event": "FRESHNESS_LOCK_RELEASED"}, separators=(",", ":")))
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        LOGGER.warning("Could not explicitly release freshness session lock: %s", type(exc).__name__)


def _install_shutdown_handlers(stop_event: threading.Event) -> dict[int, Any]:
    previous: dict[int, Any] = {}
    if threading.current_thread() is not threading.main_thread():
        return previous
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, lambda _signum, _frame, event=stop_event: event.set())
    return previous


def _restore_shutdown_handlers(previous: Mapping[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _failure_line(exc: Exception, clock: Callable[[], datetime]) -> str:
    try:
        timestamp = _iso_utc(clock())
    except Exception:
        timestamp = None
    message = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
    return json.dumps({"event": "FRESHNESS_PASS_FAILED", "timestamp": timestamp, "error": message},
                      separators=(",", ":"))


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    connect_fn: Callable[[Mapping[str, str] | None], Any] = connect_from_env,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleeper: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run one pass and exit")
    mode.add_argument("--loop", action="store_true", help="run continuously until interrupted or a pass fails")
    parser.add_argument("--interval", type=int, help="seconds between loop passes (overrides the environment)")
    args = parser.parse_args(argv)
    settings = os.environ if env is None else env
    interval = args.interval if args.interval is not None else _positive_setting(
        settings, "ANZMA_FRESHNESS_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)
    lock_retry = _positive_setting(settings, "ANZMA_FRESHNESS_LOCK_RETRY_SECONDS", DEFAULT_LOCK_RETRY_SECONDS)
    if interval < 1:
        parser.error("--interval must be at least one second")
    stop_event = threading.Event()
    previous_handlers = _install_shutdown_handlers(stop_event)
    conn = None
    lock_acquired = False
    try:
        conn = connect_fn(env)
    except Exception as exc:
        emit(_failure_line(exc, clock))
        _restore_shutdown_handlers(previous_handlers)
        return 1
    try:
        lock_acquired = acquire_instance_lock(conn, stop_event, retry_seconds=lock_retry, emit=emit)
        if not lock_acquired or stop_event.is_set():
            return 0
        if args.once:
            emit(json.dumps(run_pass(conn, clock=clock), separators=(",", ":")))
        else:
            run_loop(conn, interval=interval, clock=clock, sleeper=sleeper, emit=emit,
                     stop_event=stop_event)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        emit(_failure_line(exc, clock))
        return 1
    finally:
        if lock_acquired:
            release_instance_lock(conn, emit=emit)
        conn.close()
        _restore_shutdown_handlers(previous_handlers)


if __name__ == "__main__":
    raise SystemExit(main())
