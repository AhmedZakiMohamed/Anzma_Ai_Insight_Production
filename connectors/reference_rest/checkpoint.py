"""Durable local checkpoint and outbox for the reference connector runner.

The exact batch is committed to the outbox before Core is called. The source
cursor advances only after Core confirms acceptance. If the HTTP response is
lost after Core commits, restart resends the identical batch ID and payload.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class SyncCheckpointStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS connector_sync_state (
                source_code TEXT NOT NULL,
                building_code TEXT NOT NULL,
                from_utc TEXT NOT NULL,
                to_utc TEXT NOT NULL,
                next_cursor TEXT,
                visited_cursors TEXT NOT NULL DEFAULT '[]',
                complete_so_far INTEGER NOT NULL DEFAULT 1,
                page_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL CHECK (status IN ('RUNNING','COMPLETE','INCOMPLETE')),
                pending_batch TEXT,
                pending_next_cursor TEXT,
                pending_complete INTEGER,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source_code, building_code)
            )""")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def exclusive_run(self) -> Iterator[None]:
        """Prevent two local processes from advancing the same SQLite checkpoint concurrently."""
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+b") as handle:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError):
                raise RuntimeError("another connector run is active for this local state file") from None
            try:
                yield
            finally:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def begin(self, source_code: str, building_code: str, start: str, end: str) -> bool:
        """Return False when this exact range was already completed."""
        with self._connect() as conn:
            row = conn.execute("""SELECT * FROM connector_sync_state
                WHERE source_code=? AND building_code=?""", (source_code, building_code)).fetchone()
            if row and row["status"] == "RUNNING" and (row["from_utc"], row["to_utc"]) != (start, end):
                raise RuntimeError("another sync range is incomplete; resume it before starting a different range")
            if row and row["status"] == "RUNNING":
                # Resume without clearing the durable cursor or outbox.
                return True
            if row and row["status"] == "COMPLETE" and (row["from_utc"], row["to_utc"]) == (start, end):
                return False
            conn.execute("""INSERT INTO connector_sync_state
                (source_code,building_code,from_utc,to_utc,next_cursor,visited_cursors,
                 complete_so_far,page_count,status,pending_batch,pending_next_cursor,pending_complete,updated_at)
                VALUES (?,?,?,?,NULL,'[]',1,0,'RUNNING',NULL,NULL,NULL,CURRENT_TIMESTAMP)
                ON CONFLICT (source_code,building_code) DO UPDATE SET
                  from_utc=excluded.from_utc,to_utc=excluded.to_utc,next_cursor=NULL,
                  visited_cursors='[]',complete_so_far=1,page_count=0,status='RUNNING',
                  pending_batch=NULL,pending_next_cursor=NULL,pending_complete=NULL,
                  updated_at=CURRENT_TIMESTAMP""", (source_code, building_code, start, end))
            return True

    def state(self, source_code: str, building_code: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("""SELECT * FROM connector_sync_state
                WHERE source_code=? AND building_code=?""", (source_code, building_code)).fetchone()
        if row is None:
            raise RuntimeError("sync checkpoint was not initialized")
        result = dict(row)
        result["visited_cursors"] = json.loads(result["visited_cursors"])
        result["pending_batch"] = json.loads(result["pending_batch"]) if result["pending_batch"] else None
        return result

    def save_pending(self, source_code: str, building_code: str, requested_cursor: str | None,
                     next_cursor: str | None, complete: bool, batch: dict[str, Any]) -> None:
        with self._connect() as conn:
            row = conn.execute("""SELECT visited_cursors,pending_batch FROM connector_sync_state
                WHERE source_code=? AND building_code=? AND status='RUNNING'""",
                               (source_code, building_code)).fetchone()
            if row is None:
                raise RuntimeError("sync is not running")
            if row["pending_batch"] is not None:
                raise RuntimeError("a previous Core batch must be acknowledged before fetching another page")
            visited = json.loads(row["visited_cursors"])
            if requested_cursor is not None and requested_cursor not in visited:
                visited.append(requested_cursor)
            conn.execute("""UPDATE connector_sync_state SET visited_cursors=?,pending_batch=?,
                pending_next_cursor=?,pending_complete=?,updated_at=CURRENT_TIMESTAMP
                WHERE source_code=? AND building_code=?""",
                         (json.dumps(visited), json.dumps(batch, ensure_ascii=False, separators=(",", ":")),
                          next_cursor, int(complete), source_code, building_code))

    def advance_after_acceptance(self, source_code: str, building_code: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("""SELECT pending_batch,pending_next_cursor,pending_complete,
                complete_so_far,page_count FROM connector_sync_state
                WHERE source_code=? AND building_code=? AND status='RUNNING'""",
                               (source_code, building_code)).fetchone()
            if row is None or row["pending_batch"] is None:
                raise RuntimeError("there is no pending batch to acknowledge")
            complete = bool(row["complete_so_far"] and row["pending_complete"])
            cursor = row["pending_next_cursor"]
            status = "RUNNING" if cursor is not None else ("COMPLETE" if complete else "INCOMPLETE")
            conn.execute("""UPDATE connector_sync_state SET next_cursor=?,complete_so_far=?,
                page_count=page_count+1,status=?,pending_batch=NULL,pending_next_cursor=NULL,
                pending_complete=NULL,updated_at=CURRENT_TIMESTAMP
                WHERE source_code=? AND building_code=?""",
                         (cursor, int(complete), status, source_code, building_code))
            return {"next_cursor": cursor, "complete": complete, "status": status,
                    "page_count": row["page_count"] + 1}
