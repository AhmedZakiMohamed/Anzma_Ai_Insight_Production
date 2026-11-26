# API_CONTRACT.md - Anzma Unified Platform

**Owner:** Amr (reviewed by: Ahmed - `[TBD: confirm]`)
**Status:** Historical Day 2 platform API proposal; not the authoritative ingestion contract. The approved versioned ingestion API is implemented at `/api/v1/ingest/batches` and documented in [`INGESTION_CONTRACT.md`](INGESTION_CONTRACT.md). Other proposed routes below remain proposals unless independently verified.
**Base path in this draft:** `/api/v1` **[Proposed for this historical platform API]**. Existing dashboard, alert, chat, and incident paths remain supported.

Tags: **[Verified]** exists in code today, **[Proposed]** agreed by this document, **[TBD]** open.

## 1. Conventions

| Topic | Rule |
|---|---|
| Format | JSON, UTF-8. |
| Time | ISO 8601 UTC with `Z`, for example `2026-11-05T06:00:00Z`. Naive timestamps are rejected (Ahmed's current code treats them as UTC; the contract is stricter). |
| Window | `hours` in `{1, 6, 12, 24}`, anchored to the latest event in the dataset. `[TBD: add 72/168 for history questions]` |
| System codes | `HVAC, NETWORK, ACCESS, CCTV, FIRE_ALARM`. |
| Severity | `INFO, WARNING, CRITICAL`. |
| Pagination | `limit` (1-1000, default 100) and `offset` (>= 0) on list endpoints **[Verified]** in Amr's code. |
| Auth | None in Day 2 (input validation only). |

## 2. Error format **[Proposed]**

```json
{
  "error": {
    "code": "BATCH_DUPLICATE",
    "message": "Batch rejected: 2 duplicate records.",
    "details": [{"index": 4, "asset_id": 3, "ts": "2026-11-05T05:00:00Z"}]
  }
}
```

| HTTP | `code` | When |
|---|---|---|
| 400 | `BAD_REQUEST` | Malformed parameters. |
| 404 | `NOT_FOUND` | Unknown id. |
| 404 | `NO_MATCHING_RECORDS` | The query is valid but returns nothing. |
| 409 | `BATCH_DUPLICATE` | Any record already exists. Nothing is stored. |
| 422 | `VALIDATION_ERROR` | Invalid field, unknown asset, future timestamp, event type not allowed for the asset's system. Nothing is stored. |
| 503 | `DATA_UNAVAILABLE` | Database not reachable or dataset missing. Different from "no matching records". |
| 503 | `MODEL_UNAVAILABLE` | Only if no fallback can answer. Normally the assistant falls back and returns 200. |

## 3. Ingestion

### POST `/api/v1/telemetry/batch` and `/api/v1/events/batch`

Atomic: all records are stored or none. 1 to 5000 records. Timestamps later than now + 5 minutes are rejected **[Verified]** (Ahmed); duplicates return 409 **[Verified]** (Amr).

Request (events):

```json
{
  "events": [
    {
      "asset_id": 7,
      "ts": "2026-11-05T06:00:00Z",
      "event_type": "LINK_DOWN",
      "severity": "CRITICAL",
      "value": null,
      "unit": null,
      "details": {"port": "Gi0/1"}
    }
  ]
}
```

Request (telemetry):

```json
{
  "records": [
    {"asset_id": 3, "metric": "supply_air_temp", "ts": "2026-11-05T06:00:00Z", "value": 24.6}
  ]
}
```

Response 201:

```json
{"inserted": 1}
```

`asset_name` may be used instead of `asset_id` **[Verified]** (Ahmed). `[TBD: keep both?]`

## 4. Retrieval

### GET `/api/v1/telemetry` and `/api/v1/events`

Query: `asset_id`, `location_id`, `system_type`, `start_time`, `end_time`, `limit`, `offset` **[Verified]** (Amr).

Response 200:

```json
{
  "count": 1,
  "limit": 100,
  "offset": 0,
  "items": [
    {
      "id": 1042,
      "asset": {"id": 7, "name": "SW-CORE-01", "system_type": "NETWORK"},
      "location": {"id": 2, "building": "HQ", "floor": 1, "zone": "Server Room"},
      "ts": "2026-11-05T06:00:00Z",
      "event_type": "LINK_DOWN",
      "severity": "CRITICAL"
    }
  ]
}
```

### GET `/api/v1/assets`, `/api/v1/locations`, `/api/v1/systems`, `/api/v1/health`

Reference lists and a health probe **[Verified]** (Ahmed) moved under the prefix.

## 5. Dashboard

| Endpoint | Purpose | Source |
|---|---|---|
| GET `/dashboard/filters` | Available systems, locations, window sizes | Amr **[Verified]** |
| GET `/dashboard/overview` | KPIs and system status | Amr (name), logic merged **[Proposed]** |
| GET `/dashboard/trends` | Time series | Ahmed **[Verified]** |
| GET `/dashboard/equipment-health` | Per-asset status | Ahmed **[Verified]** |

Parameters: `hours`, `system_type`, `location_id` (optional).

Response of `/dashboard/overview`:

```json
{
  "window": {"from": "2026-11-04T06:00:00Z", "to": "2026-11-05T06:00:00Z", "hours": 24},
  "kpis": [
    {"key": "critical_alerts", "name": "Critical alerts", "value": 5, "unit": "count", "applicable": true}
  ],
  "systems": [
    {"system_type": "HVAC", "status": "CRITICAL", "assets": 3}
  ]
}
```

`status` is one of `OK, WARNING, CRITICAL, NO_DATA` (`NO_DATA` is new, **[Proposed]**). Each KPI is defined once in `KPI_DEFINITIONS.md`.

## 6. Alerts

| Endpoint | Purpose | Source |
|---|---|---|
| POST `/alerts/run?hours=` | Evaluate rules over a window | Ahmed **[Verified]** |
| GET `/alerts` | List; filters `severity`, `status`, `system_type`, `hours` | Ahmed **[Verified]** |
| GET `/alerts/{id}` | Alert with evidence, explanation, recommendations | Ahmed **[Verified]** |
| GET `/alerts/{id}/explain` | Stored explanation | Ahmed **[Verified]** |
| POST `/alerts/{id}/explain` | Generate explanation | Ahmed **[Verified]** |
| PATCH `/alerts/{id}` | Change status `OPEN -> ACKNOWLEDGED -> RESOLVED` | New **[Proposed]** |

Alert object:

```json
{
  "id": 41,
  "rule_id": "HVAC_HIGH_TEMP",
  "detection_method": "THRESHOLD",
  "severity": "CRITICAL",
  "status": "OPEN",
  "asset": {"id": 3, "name": "HVAC-CRAC-01", "system_type": "HVAC"},
  "title": "HVAC temperature above threshold",
  "summary": "...",
  "first_ts": "2026-11-05T04:30:00Z",
  "last_ts": "2026-11-05T06:00:00Z",
  "evidence": [{"ref": "E1", "event_id": 9001, "ts": "2026-11-05T05:10:00Z", "value": 34.1}],
  "incident_id": 12
}
```

`incident_id` is new **[Proposed]**. The asset names above are illustrative; real names come from the database.

## 7. Incidents **[Proposed]** (new)

| Endpoint | Purpose |
|---|---|
| GET `/incidents` | List ranked incidents; filters `status`, `severity`, `hours` |
| GET `/incidents/{id}` | One incident with its alerts, assets, locations and recommended actions |

```json
{
  "id": 12,
  "status": "OPEN",
  "severity": "CRITICAL",
  "rank": 1,
  "title": "Server-room cooling failure",
  "location": {"id": 2, "zone": "Server Room"},
  "first_ts": "2026-11-05T04:30:00Z",
  "last_ts": "2026-11-05T06:00:00Z",
  "alert_ids": [41, 42],
  "grouping_rule": "same_location_within_15_min",
  "recommended_actions": [{"id": "HVAC_VERIFY", "priority": 1, "text": "Confirm the reading on site..."}]
}
```

Incident IDs are stable (stored). Today Amr's incidents are addressed by list index **[Verified]**; this fixes that.

## 8. Assistant

### POST `/api/v1/assistant/chat`

Request:

```json
{
  "question": "What is wrong with the building?",
  "session_id": "b2f7c1e0-...",
  "hours": 24,
  "model": "qwen",
  "scenario": null
}
```

| Field | Rule |
|---|---|
| `question` | 1-500 characters (both sides use 500) **[Verified]**. |
| `session_id` | Optional. Server-side memory is keyed by it. If missing, a new one is returned. |
| `hours` | `{1, 6, 12, 24}`. |
| `model` | `qwen` or `llama` (Ollama models). |
| `scenario` | Optional. Day 2: filters to a scenario's time window from `scenario_runs`. `[TBD: Ahmed]` |
| history | **Not sent by the client.** Today the browser sends the last 8 messages **[Verified]**; memory moves to the server **[Proposed]**. |

Response 200:

```json
{
  "session_id": "b2f7c1e0-...",
  "answer": "The server-room cooling unit reports a critical temperature. [F2]",
  "language": "en",
  "tool_used": "get_recent_faults",
  "model_used": "qwen2.5:3b",
  "ai_source": "llm",
  "data_sufficiency": "complete",
  "window": {"from": "2026-11-04T06:00:00Z", "to": "2026-11-05T06:00:00Z", "hours": 24},
  "evidence": [{"ref": "F2", "text": "HVAC-CRAC-01 ... ", "data": {"asset": "HVAC-CRAC-01"}}],
  "references": {"assets": [3], "incidents": [12]},
  "validation": {
    "status": "validated",
    "checks_run": ["citation_exists", "number_in_evidence", "time_in_evidence", "date_in_evidence", "asset_name_exact", "required_fact_cited", "judgment_phrase"],
    "errors": [],
    "attempts": 1
  },
  "limitations": []
}
```

| Field | Values |
|---|---|
| `ai_source` | `llm` (model answer passed validation) or `fallback` (deterministic facts) or `rules` (fixed text, for example out of scope). Today Amr's code always returns `llm` **[Verified]**; the contract makes `fallback` real. |
| `data_sufficiency` | `complete`, `partial`, `no_data`. |
| `validation.status` | `validated`, `not_validated`, `failed`. **`validated` only when `checks_run` is non-empty and every check passed.** |
| `checks_run` | Only checks that actually ran. The seven names above are the ones implemented in Ahmed's validator **[Verified]**; the contract may not list others. |
| `limitations` | Plain-text statements about what the answer cannot cover (for example "no energy readings in the dataset"). |

Out-of-scope example: `ai_source = "rules"`, `validation.status = "not_validated"`, `data_sufficiency = "no_data"`, with a fixed refusal text.

Tools behind this endpoint **[Proposed]**: `get_building_status`, `get_recent_faults`, `get_cctv_availability`, `get_network_issues`, `get_corrective_priorities`, `get_energy_consumption_change` (Amr's six **[Verified]**), implemented by Ahmed's collectors (`c_status`, `c_faults`, `c_cameras`, `c_network`, `c_repair`, `c_energy`) **[Verified]**. Optional later: `get_kpis`, `get_history` (from `c_kpi`, `c_history`).

### GET `/api/v1/assistant/questions`

Suggested questions for UI buttons **[Verified]** (Ahmed's `/chat/questions`, renamed).

## 8. Assistant chat contract

The widget chat endpoint returns a JSON object shaped like this:

```json
{
  "answer": "There are 8 critical alerts in the database.",
  "language": "en",
  "source": "DATABASE",
  "grounding": {"passed": true},
  "sources": [{"ref": "Q1", "text": "verified in database: ..."}],
  "data_sufficient": true,
  "window": {"hours": 24},
  "session_state": {"last_alert_id": 42}
}
```

`source` is one of:

- `DATABASE`: the answer was derived from a verified read-only SQL query against the application database.
- `FALLBACK`: the answer is still grounded in database evidence, but the app did not find a dedicated direct answer path and used the fallback logic.
- `RULES`: the reply is intentionally not a database fact; it is a guidance rule explaining that the request is unsupported or needs more detail.

`grounding.passed` must be computed from actual query results, not a hard-coded constant. `True` means the response numbers/timestamps/assets came back from the database or a validated fallback path; `False` means the answer is intentionally non-factual guidance.

The widget sends `history` and `session_state` on every request and reuses them for follow-up phrases such as `it`, `that`, or Arabic `ده`.

## 9. Module interfaces (Python)

```python
class ModelProvider(Protocol):
    name: str
    def generate_json(self, messages: list[dict], *, temperature: float = 0) -> dict: ...

class Tool(Protocol):
    name: str
    description: str
    def run(self, cur, *, hours: int, lang: str, **kw) -> "ToolResult": ...

@dataclass
class ToolResult:
    facts: list[dict]          # [{"ref": "F1", "text": str, "data": dict}]
    sufficient: bool
    window: dict
    references: dict           # {"assets": [...], "incidents": [...]}

class Validator(Protocol):
    def check(self, text: str, facts: list[dict]) -> "ValidationResult": ...  # status, checks_run, errors
```

`OllamaProvider` is the only implementation in the default build.

## 10. Open items

1. Window sizes beyond `{1, 6, 12, 24}`. `[TBD]`
2. `scenario` parameter semantics. `[TBD]`
3. Keep `asset_name` as an alternative to `asset_id` in ingestion. `[TBD]`
4. Whether unprefixed aliases stay on Day 2. `[TBD]`

## E5. Implemented read-only freshness API

This section documents the current E5 routes; it supersedes no ingestion behavior. The versioned ingestion contract remains [`INGESTION_CONTRACT.md`](INGESTION_CONTRACT.md). These routes are read-only, require the operator bearer token or authenticated session cookie, and do not accept source ingestion keys. They return UTC timestamps ending in `Z`, use codes instead of database IDs, and do not change existing dashboard, KPI, alert, equipment-health, or chat response shapes.

All list routes accept `limit` (default 100, valid 1–500) and `offset` (default 0, non-negative). Responses include `total` and `next_offset` (`null` at the end). Missing/invalid operator credentials return HTTP 401; source ingestion keys are not accepted. Invalid enum filters, limits, offsets, or timestamps return HTTP 400. An unknown code filter is valid and returns an empty list. Database errors return HTTP 503. Non-GET methods are not defined for these routes.

### GET `/api/v1/freshness`

Filters: `building_code`, `source_code`, `asset_code`, `metric_code`, and `state` (`FRESH`, `STALE`, `MISSING`, `UNKNOWN`). Retired and disabled assets are excluded. The response includes `evaluated_at`, integer `evaluation_age_seconds`, `snapshot_stale`, `state_counts`, paging fields, and `items`. Each item contains building/source/asset codes, `record_kind`, metric/event code, freshness state, `last_observed_at`, `last_received_at`, `age_seconds`, expected interval, evaluation timestamp, and nullable `open_gap` evidence.

An absent evaluation returns null evaluation time/age, an empty item list when there are no current rows, and `snapshot_stale: true`. `ANZMA_FRESHNESS_SNAPSHOT_MAX_AGE` defaults to 180 seconds. A valid positive integer overrides it; invalid or non-positive values log one warning and use 180 seconds.

### GET `/api/v1/sources/liveness`

Filters: `building_code`, `source_code`, and `state` (`ALIVE`, `SILENT`, `UNKNOWN`, `DISABLED`). Each item contains source/building codes, state and reason, last-received time and age, expected keepalive interval, active/stale/missing series counts, mass-stale flag, and separate `last_success_at`, `last_error_at`, `last_error`, and `connector_status` evidence. Receipt and quality evidence do not get folded together: an all-rejected batch can keep liveness `ALIVE` while success/error fields show rejection. `last_error` is capped at 500 characters and `last_error_truncated` reports truncation. `connector_status` is serialized and bounded to 8 KiB; if it is malformed or larger, the field is null and `connector_status_omitted` is true. Consumers must treat it as untrusted diagnostic JSON.

### GET `/api/v1/data-gaps`

Filters: `status` (`open`, `closed`, `all`; default `open`), `cause` (`UNKNOWN`, `NO_DATA`, `SOURCE_SUSPECTED`), `building_code`, `source_code`, `asset_code`, and `since` (RFC 3339 timestamp with timezone; selects `detected_at >= since`). The top-level response echoes the selected status. Each item contains public codes, series type, gap boundaries, detection/closure times, expected interval, `opened_in_work_hours`, cause and update time, and ordered cause history (`old_cause`, `new_cause`, `changed_at`). Database gap IDs are not returned. Closed-gap causes and boundaries are presented as persisted historical evidence.

The dashboard has one additive **Data freshness & sources** panel. It reads these three routes when the existing Refresh action runs. It displays UTC timestamps, marks stale snapshots, escapes all dynamic text, and presents source/gap classifications as evidence rather than confirmed equipment faults. No polling loop, AI/chat integration, equipment-state change, or alert behavior is added by E5.

## Universal platform milestone 1 — deterministic operational analytics

**Status:** Implemented in the working tree; acceptance against a real external system is pending. The synthetic reference REST integration has been tested separately. These routes require operator authentication (bearer token or authenticated session cookie); connector source credentials are not accepted.

### POST `/api/v1/analytics/execute`

Runs one typed deterministic analysis over records already ingested into Core. It does not ask the language model to calculate results.

Request:

```json
{
  "analysis": "doors.after_hours",
  "start_date": "2026-11-01",
  "end_date": "2026-11-07",
  "building_code": "MAIN-TOWER",
  "after_local": "20:00:00"
}
```

| Field | Rule |
|---|---|
| `analysis` | Required: `doors.after_hours` or `devices.offline_frequency`. |
| `start_date`, `end_date` | Required ISO calendar dates; inclusive. `end_date` must not precede `start_date`; the range is limited to 366 calendar days. |
| `building_code` | Optional registered building code. Omitted means all registered buildings. |
| `after_local` | Time without a timezone, used only for `doors.after_hours`; defaults to `20:00`. Door dates and clock time use each building's IANA timezone. |

The offline analysis counts explicit `offline_start` transitions in the building-local date window. Repeated starts during one open outage count once; only `offline_end` closes an outage. A preceding transition is considered to determine whether an outage was already active at the start of the window. Silence is not treated as an offline event.

Response 200 contains `result_id`, `tool`, normalized `parameters`, `generated_at` (UTC), `source_ids`, `trace_id`, `query_version`, `columns`, `rows`, `total_rows`, `truncated`, and `limitations`. Door rows include the building, door/asset and location, observed UTC and local times, timezone, and event evidence references. Offline rows include the building, device/asset, incident count, and the `offline_start` evidence references.

`completeness` is currently `UNKNOWN` and `complete` is `null`: the response covers stored Core records and does not claim that the upstream source supplied complete history. `quality` is `SOURCE_RECORDS`. Door results are capped at 10,000 rows and set `truncated` when capped. Database errors return 503; invalid dates, unknown building codes, invalid timezones, or unsupported ranges return 422; missing operator credentials return 401.

### Deterministic analytics through `POST /chat`

The existing chat route uses the same typed analyses for recognized after-hours-door and offline-frequency questions. When enabled, the model can select only `analyze_after_hours_doors` or `rank_offline_devices` and supply validated parameters; it receives no SQL tool on this path. Tool dates must match the first two explicit ISO dates (`YYYY-MM-DD`) in the request. Door threshold time is parsed from the request; without a stated time it defaults to 20:00 local. The Core analytics service executes the calculation. A model timeout, invalid tool call, or `use_llm: false` uses the deterministic parser. The chat response includes at most 200 result rows and at most 50 top-level event citations; the result's row-level evidence is retained for returned rows. The answer states that upstream history completeness is unverified. If dates are missing or the request is outside supported bounds, the route asks for the required dates or returns a deterministic limitation rather than inventing a result. The typed API and chat figures should agree for equivalent normalized parameters.

The typed API and chat share the same deterministic service. Their figures and event evidence should agree when the typed API is called for all buildings and uses the default 20:00 threshold. The working-tree acceptance tests verify that parity against the synthetic fixture; real-source parity remains pending.

## Alert stream replay

`GET /api/v1/alerts/stream/latest` returns `latest_event_id`, a starting watermark for a newly connected client. `GET /api/v1/alerts/stream?after_id=<id>&limit=<1..500>` returns persisted alerts with greater IDs in ascending order, plus `next_after_id` and `has_more`. The existing operator authentication applies. The Socket.IO `new_alert` payload and the replay item use the persisted alert `id` as their event cursor; clients deduplicate by that ID and request replay after reconnect. The API is read-only and adds no second alert store.

This contract replays newly stored alert records and returns their current persisted status. It does not provide a separate audit event for every status transition; a durable transition history requires an approved data-model change. Independent reconnect/backfill acceptance remains pending.

## G1 connector setup preview

`POST /api/v1/connectors/test-connection` accepts the `anzma.reference-rest` connector ID, endpoint origin, and bearer credential. It requires the normal Core operator session/bearer authentication. Only exact origins listed in `ANZMA_CONNECTOR_ALLOWED_ORIGINS` are permitted; HTTP also requires the origin in `ANZMA_CONNECTOR_ALLOW_HTTP_ORIGINS`. The test is read-only, rejects redirects, returns connection status/latency/capabilities, and does not persist the submitted credential. This is a synthetic reference preview only: it does not register a source or initiate ingestion and it is not a vendor support-tier qualification.
