from __future__ import annotations

import uuid
from datetime import date, time, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _result(tool: str, parameters: dict, rows: list[dict], limitations: list[str], *, total_rows: int | None = None) -> dict:
    source_ids = sorted({ref["source_code"] for row in rows
                         for ref in row.get("evidence_refs", []) if ref.get("source_code")})
    schemas = {
        "doors.after_hours": [
            {"key": "building_code", "type": "string"}, {"key": "door", "type": "string"},
            {"key": "location", "type": "string"}, {"key": "observed_at_local", "type": "datetime"},
            {"key": "timezone", "type": "string"}, {"key": "evidence_refs", "type": "array"},
        ],
        "devices.offline_frequency": [
            {"key": "building_code", "type": "string"}, {"key": "device", "type": "string"},
            {"key": "offline_incidents", "type": "integer"}, {"key": "evidence_refs", "type": "array"},
        ],
    }
    return {
        "result_id": "res_" + uuid.uuid4().hex,
        "tool": tool,
        "parameters": parameters,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_ids": source_ids,
        "trace_id": uuid.uuid4().hex,
        "query_version": "0.1",
        "columns": schemas[tool],
        "complete": None,
        "completeness": "UNKNOWN",
        "quality": "SOURCE_RECORDS",
        "truncated": False,
        "rows": rows,
        "total_rows": len(rows) if total_rows is None else total_rows,
        "limitations": limitations + [
            "Coverage is limited to records currently stored in Core; upstream history completeness is not yet tracked."
        ],
    }


def doors_after_hours(cur, start_date: date, end_date: date, *, building_code: str | None = None,
                      after_local: time = time(20, 0)) -> dict:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if (end_date - start_date).days > 365:
        raise ValueError("date range is limited to 366 calendar days")
    if start_date == end_date and after_local >= time(23, 59, 59, 999999):
        raise ValueError("after_local leaves no time in the selected date")
    _validate_building_timezones(cur, building_code)
    cur.execute(
        """SELECT e.event_id, e.source_event_id, e.ts, e.severity, e.details,
                  a.asset_code, a.name AS asset_name, b.building_code, b.name AS building_name,
                  b.timezone, l.name AS location_name, ds.source_code,
                  e.ts AT TIME ZONE b.timezone AS local_ts, COUNT(*) OVER() AS result_total
             FROM event_records e
             JOIN event_types et USING (event_type_id)
             JOIN assets a USING (asset_id)
             JOIN locations l USING (location_id)
             JOIN buildings b USING (building_id)
             LEFT JOIN data_sources ds USING (source_id)
            WHERE et.name = 'door_opened'
              AND (%s::text IS NULL OR b.building_code = %s)
              AND (e.ts AT TIME ZONE b.timezone)::date BETWEEN %s AND %s
              AND (e.ts AT TIME ZONE b.timezone)::time >= %s
            ORDER BY b.building_code, a.asset_code, e.ts, e.event_id
            LIMIT 10001""",
        (building_code, building_code, start_date, end_date, after_local),
    )
    fetched = cur.fetchall()
    truncated = len(fetched) > 10000
    rows = []
    for row in fetched[:10000]:
        rows.append({
            "building_code": row["building_code"], "building": row["building_name"],
            "asset_code": row["asset_code"], "door": row["asset_name"], "location": row["location_name"],
            "observed_at_utc": row["ts"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "observed_at_local": row["local_ts"].isoformat(), "timezone": row["timezone"],
            "evidence_refs": [{"event_id": row["event_id"], "source_event_id": row["source_event_id"],
                               "source_code": row["source_code"]}],
        })
    result = _result("doors.after_hours", {"start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(), "after_local": after_local.isoformat(timespec="minutes"),
                    "building_code": building_code}, rows, [
                        "Only explicit door_opened events are counted; access_granted is not physical-open evidence."
                    ], total_rows=int(fetched[0]["result_total"]) if fetched else 0)
    result["truncated"] = truncated
    if truncated:
        result["limitations"].append("Result was capped at 10000 rows.")
    return result


def offline_frequency(cur, start_date: date, end_date: date, *, building_code: str | None = None) -> dict:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if (end_date - start_date).days > 365:
        raise ValueError("date range is limited to 366 calendar days")
    _validate_building_timezones(cur, building_code)
    cur.execute(
        """WITH buildings_scope AS (
                 SELECT building_id, building_code, name, timezone
                   FROM buildings
                  WHERE (%s::text IS NULL OR building_code = %s)
             ), bounds AS (
                 SELECT *, (start_date::timestamp AT TIME ZONE timezone) AS start_utc,
                           ((end_date + 1)::timestamp AT TIME ZONE timezone) AS end_utc
                   FROM buildings_scope CROSS JOIN (SELECT %s::date AS start_date, %s::date AS end_date) d
             ), prior AS (
                 SELECT DISTINCT ON (e.asset_id) e.event_id, e.source_event_id, e.asset_id,
                        e.ts, et.name AS event_type, bd.building_code, bd.start_utc, bd.end_utc
                   FROM bounds bd JOIN locations l USING (building_id)
                   JOIN assets a USING (location_id) JOIN event_records e USING (asset_id)
                   JOIN event_types et USING (event_type_id)
                  WHERE et.name IN ('offline_start','offline_end') AND e.ts < bd.start_utc
                  ORDER BY e.asset_id, e.ts DESC, e.event_id DESC
             ), selected AS (
                 SELECT e.event_id, e.source_event_id, e.asset_id, e.ts, et.name AS event_type,
                        bd.building_code, bd.start_utc, bd.end_utc
                   FROM bounds bd JOIN locations l USING (building_id)
                   JOIN assets a USING (location_id) JOIN event_records e USING (asset_id)
                   JOIN event_types et USING (event_type_id)
                  WHERE et.name IN ('offline_start','offline_end')
                    AND e.ts >= bd.start_utc AND e.ts < bd.end_utc
             ), transitions AS (
                 SELECT * FROM prior UNION ALL SELECT * FROM selected
             )
             SELECT t.event_id, t.source_event_id, t.asset_id, t.ts, t.event_type,
                    t.building_code, a.asset_code, a.name AS asset_name, ds.source_code,
                    (t.ts >= t.start_utc) AS in_window
               FROM transitions t JOIN assets a USING (asset_id)
               LEFT JOIN event_records e USING (event_id)
               LEFT JOIN data_sources ds USING (source_id)
              ORDER BY t.asset_id, t.ts, t.event_id""",
        (building_code, building_code, start_date, end_date),
    )
    events = cur.fetchall()
    counts: dict[int, dict] = {}
    active: dict[int, bool] = {}
    for event in events:
        aid = event["asset_id"]
        if event["event_type"] == "offline_start":
            if not active.get(aid, False):
                active[aid] = True
                if event["in_window"]:
                    item = counts.setdefault(aid, {"building_code": event["building_code"],
                        "asset_code": event["asset_code"], "device": event["asset_name"],
                        "offline_incidents": 0, "evidence_refs": []})
                    item["offline_incidents"] += 1
                    item["evidence_refs"].append({"event_id": event["event_id"],
                                                   "source_event_id": event["source_event_id"],
                                                   "source_code": event["source_code"]})
        elif event["event_type"] == "offline_end":
            active[aid] = False
    rows = sorted(counts.values(), key=lambda row: (-row["offline_incidents"], row["asset_code"]))
    return _result("devices.offline_frequency", {"start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(), "building_code": building_code}, rows, [
                        "Repeated offline_start alarms during an open outage count once; only explicit offline_end closes it.",
                        "Missing recovery leaves the outage open; silence is never treated as an offline transition."
                    ])


def _validate_building_timezones(cur, building_code: str | None) -> None:
    cur.execute(
        """SELECT building_code, timezone FROM buildings
             WHERE (%s::text IS NULL OR building_code = %s)
             ORDER BY building_code""", (building_code, building_code))
    buildings = cur.fetchall()
    if building_code and not buildings:
        raise ValueError("building_code is not registered")
    if not buildings:
        raise ValueError("no buildings are registered")
    for row in buildings:
        if not row["timezone"]:
            raise ValueError("building timezone is missing; local-date analytics cannot be computed")
        try:
            ZoneInfo(row["timezone"])
        except (ZoneInfoNotFoundError, TypeError):
            raise ValueError(f"building {row['building_code']} has an invalid IANA timezone") from None
