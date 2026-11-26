"""Cursor-based replay for the existing persisted alert stream."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from psycopg import OperationalError

from app.api.frontend_api import ALERT_SQL, _alert
from app.repositories import dashboard_core_pg as core


def create_router(open_ro=core.open_readonly):
    router = APIRouter(prefix="/api/v1/alerts", tags=["Alert stream"])

    @router.get("/stream/latest", summary="Get the current alert replay watermark")
    def latest_alert_cursor():
        try:
            with open_ro() as conn:
                row = conn.execute("SELECT COALESCE(MAX(alert_id), 0) AS latest_event_id FROM alerts").fetchone()
        except OperationalError as exc:
            raise HTTPException(503, "Alert stream is temporarily unavailable") from exc
        return {"latest_event_id": row["latest_event_id"]}

    @router.get("/stream", summary="Backfill persisted alerts after an event ID")
    def replay_alerts(after_id: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500)):
        """Return alerts oldest-first so a client can resume from its last stored alert ID."""
        try:
            with open_ro() as conn:
                rows = conn.execute(
                    ALERT_SQL + " WHERE al.alert_id > %s ORDER BY al.alert_id ASC LIMIT %s",
                    (after_id, limit + 1),
                ).fetchall()
        except OperationalError as exc:
            raise HTTPException(503, "Alert stream is temporarily unavailable") from exc

        has_more = len(rows) > limit
        items = [_alert(row) for row in rows[:limit]]
        next_after_id = items[-1]["id"] if items else after_id
        return {"items": items, "after_id": after_id, "next_after_id": next_after_id,
                "has_more": has_more}

    return router
