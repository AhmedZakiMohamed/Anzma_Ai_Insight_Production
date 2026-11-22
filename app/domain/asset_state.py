"""Pure G3 shadow asset-state derivation; never writes operational health."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable


OPERATIONAL_BY_BUCKET = {
    "critical": "CRITICAL",
    "warning": "WARNING",
    "normal": "NORMAL",
    "unknown": "UNKNOWN",
    "no_data": "UNKNOWN",
}


def derive_asset_state(
    dashboard_bucket: str,
    series: Iterable[dict[str, Any]],
    availability: str | None,
    evaluated_at: datetime,
    all_source_ids: Iterable[int] | None = None,
) -> dict[str, Any]:
    """Derive dimensions from existing dashboard bucket, Core events, and series freshness.

    Connector-reported availability is intentionally not an input. `evaluated_at` is
    required so state_since and tests are deterministic.
    """
    rows = list(series)
    operational = OPERATIONAL_BY_BUCKET.get(str(dashboard_bucket).lower(), "UNKNOWN")
    source_values = [row.get("source_id") for row in rows]
    source_ids = {value for value in source_values if value is not None}
    if all_source_ids is not None:
        source_ids.update(value for value in all_source_ids if value is not None)
    attribution_ambiguous = (len(source_ids) > 1 or (bool(source_ids) and any(v is None for v in source_values)))
    if attribution_ambiguous:
        communication = "UNKNOWN"
    else:
        freshness = {str(row.get("freshness", "UNKNOWN")).upper() for row in rows}
        freshness.discard("NOT_APPLICABLE")
        if "MISSING" in freshness:
            communication = "SILENT"
        elif "STALE" in freshness:
            communication = "DEGRADED"
        elif freshness and freshness == {"FRESH"} and source_ids:
            communication = "COMMUNICATING"
        else:
            communication = "UNKNOWN"
    availability_value = availability if availability in {"AVAILABLE", "UNAVAILABLE"} else "UNKNOWN"
    return {
        "operational_state": operational,
        "availability": availability_value,
        "communication_status": communication,
        "evaluated_at": evaluated_at,
        "state_since": evaluated_at,
        "reason_code": "DASHBOARD_BUCKET_" + str(dashboard_bucket).upper(),
        "reason_message": "Shadow projection; existing dashboard status remains authoritative.",
        "source_ids": sorted(source_ids),
        "attribution_ambiguous": attribution_ambiguous,
    }
