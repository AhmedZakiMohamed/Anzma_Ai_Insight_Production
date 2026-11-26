from __future__ import annotations

from datetime import date, time
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.analytics import operational


class AnalyticsRequest(BaseModel):
    analysis: Literal["doors.after_hours", "devices.offline_frequency"]
    start_date: date
    end_date: date
    building_code: str | None = Field(default=None, min_length=1, max_length=64)
    after_local: time = time(20, 0)


def create_router(day2_connect, db_errors=()):
    router = APIRouter(prefix="/api/v1/analytics", tags=["Analytics"])

    @router.post("/execute")
    def execute(body: AnalyticsRequest):
        if body.end_date < body.start_date:
            raise HTTPException(422, "end_date must be on or after start_date")
        if body.after_local.tzinfo is not None:
            raise HTTPException(422, "after_local must be a timezone-free local clock time")
        try:
            with day2_connect() as conn:
                with conn.cursor() as cur:
                    if body.analysis == "doors.after_hours":
                        return operational.doors_after_hours(cur, body.start_date, body.end_date,
                            building_code=body.building_code, after_local=body.after_local)
                    return operational.offline_frequency(cur, body.start_date, body.end_date,
                        building_code=body.building_code)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except db_errors as exc:
            raise HTTPException(503, "Analytics data is temporarily unavailable") from exc

    return router
