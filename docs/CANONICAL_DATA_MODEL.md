# CANONICAL_DATA_MODEL.md — Anzma AI Insight Core

| | |
|---|---|
| **Status** | Canonical model and implementation reference. Migrations 006–009 implement ingestion and freshness configuration/storage schema; freshness evaluation and derived equipment state remain future behavior. |
| **Baseline** | Historical deployed baseline: 13 tables and 5 views through migration 005. Repository target includes migrations 006–009; see `DATABASE_BASELINE.md` and `DATA_MIGRATION_PLAN.md`. |
| **Related** | `CORE_DATA_ARCHITECTURE.md`, `HISTORIAN_DESIGN.md`, `DATA_MIGRATION_PLAN.md`, `SPRINT_1_TECHNICAL_ISSUES.md` |

Notation: **[Existing]** present before migration 006 · **[Implemented]** added by migrations 006–009/code · **[NEW]** future proposal · **[Open]** requires a separate decision.
No DDL here is a migration; the column lists are specifications for review.

## Implementation status (18–20 November 2026)

- **Implemented and runtime-verified:** multiple buildings, locations, system types and assets are represented by `buildings`, `locations`, `system_types`, and `assets`; assets reference a system and location, and locations reference a building. PostgreSQL also contains historical `telemetry_records`, `event_records`, alerts, and incidents. Telemetry/event rows do not carry explicit building IDs; building is derived through the asset/location relationship.
- **Implemented and test-verified:** alert rules and anomaly detection, incident grouping/lifecycle, evidence re-reading and grounding checks, recommendations, chat/dashboard routes, and the Day 3 bandwidth percentage correction. The current checkout's offline unit suite passed 25/25; see `PRODUCTIZATION_IMPLEMENTATION_REPORT.md` for the bounded verification record.
- **Implemented (migrations 006–009 + code):** `/api/v1/ingest/batches`, source registry and hashed credentials, source-scoped asset IDs, nullable provenance/quality columns, batch/rejection audit, append-only equipment-state facts, quality-aware telemetry view, distinct source event IDs at a shared asset/type/time, migration 008 back-date/diagnostics/audit/plausibility fields, and migration 009 building schedules/expectations/gap/current-freshness storage. Existing facts were not backfilled; migration 009 only backfills new building schedule fields. Operational readers exclude quality `BAD` while retaining legacy `NULL` quality.
- **E2 schema update (20 November 2026):** migration 009 adds nullable building schedule fields, contract-ordered `reporting_expectations`, `data_gaps`, and `series_freshness_current`, plus global telemetry/heartbeat/source defaults. Existing building schedule fields are backfilled; historical fact rows are unchanged. This does not implement an evaluator, alert/incident generation, or consumer behavior.
- **Not implemented:** freshness evaluation and gap lifecycle processing, retention policy, rollups, organization hierarchy, and migrations 006–009 application to the shared runtime database.
- **Known limitation:** the existing seed is synthetic. The current multi-building schema is a structural capability, not proof that every API, chat query, or dashboard path enforces building-level isolation.
- **Approved and implemented for contract v1 (18 November 2026):** `/api/v1/ingest/batches`; per-record outcomes committed in one batch transaction; external IDs unique per source and mapped only to assets in that source's building; per-source bearer API keys stored as hashes with revocation/rotation; first-write-wins for telemetry conflicts, with rejected payloads retained separately.

---

## 1. Design principles

1. **Evolve, don’t replace.** Existing table and column names stay. Additions are nullable-first and backward compatible.
2. **Vendor independence.** The Core stores *what happened* in standard terms. Vendor identity appears only as `data_sources`, `asset_external_ids`, manufacturer/model attributes and opaque `*_metadata` JSONB — never as Core columns that drive logic.
3. **Observed time ≠ received time.** Every fact has `ts` (observed) and `received_at` (platform clock).
4. **State ≠ freshness.** A silent device is “state last known, data stale”, never automatically “offline”.
5. **Building identity is explicit** on every operational row (`building_id`).
6. **Traceability.** Every alert/evidence row resolves to stored fact rows, which resolve to a source and batch.
7. **Explicit handling of bad data.** Invalid → rejected and logged; implausible → stored with quality `BAD`; missing → a recorded gap; duplicate → idempotent no-op; late → flagged.
8. **Configuration is data.** Units, plausible ranges, expected intervals, zone types, working hours and retention live in tables, not code constants.
9. **No cascades.** All FKs are `ON DELETE RESTRICT`.
10. **Simplicity.** Eleven new tables at most; each justified in §2.

---

## 2. Entity definitions

### 2.1 Summary

| Entity | Status | Purpose |
|---|---|---|
| `organizations` | **[NEW entity]** | Customer/owner of buildings. One default row for single-customer installs. |
| `buildings` | [Existing] + new columns | Isolation unit. |
| `locations` | [Existing] + new columns | Hierarchical physical locations (floor/zone/room). |
| `system_types` | [Existing] (CHECK relaxed) | Building-system catalogue (HVAC, CCTV, …). |
| `assets` | [Existing] + new columns | Equipment, manufacturer-independent. |
| `asset_external_ids` | **[NEW entity]** | Vendor asset IDs per source → internal asset. |
| `data_sources` | **[NEW entity]** | Connectors/simulators/manual feeds. |
| `metric_types` | [Existing] + new columns | Measurement catalogue with canonical unit and plausible range. |
| `unit_conversions` | **[NEW entity]** (optional) | Registered conversions the Core may apply (initially empty). |
| `event_types` | [Existing] + new columns | Event catalogue with class and recovery link. |
| `metric_type_system_compat`, `event_type_system_compat` | [Existing] | Which metric/event is valid on which system. |
| `telemetry_records` | [Existing] + new columns | Time-series measurements (the Historian fact table). |
| `event_records` | [Existing] + new columns | Operational events. |
| `reporting_expectations` | **[Implemented schema, 009]** | Expected interval and override per contract scope; no evaluator yet. |
| `asset_state_current` | **[NEW entity]** | One row per asset: current state + freshness. |
| `asset_state_history` | **[NEW entity]** | Append-only state transitions. |
| `data_gaps` | **[Implemented schema, 009]** | Open/closed missing-series gap audit; no writer/job yet. |
| `series_freshness_current` | **[Implemented schema, 009]** | Current per-series freshness storage; no writer/job yet. |
| `alerts` | [Existing] + new columns | Detected conditions. |
| `incidents` | [Existing] + new columns | Managed cases grouping alerts. |
| `alert_evidence` | **[NEW entity]** | FK-backed evidence rows per alert. |
| `ingestion_batches` | **[NEW entity]** | One row per received batch (idempotency, reconciliation). |
| `ingestion_rejections` | **[NEW entity]** | Rejected records (audit; minimal dead-letter). |
| `retention_policies` | **[NEW entity]** (config) | Configurable retention per data class (§ in `HISTORIAN_DESIGN.md`). |
| `scenario_runs` | [Existing] | Test harness; to move to an `eval` schema later. |

### 2.2 Event vs Alert vs Incident

| | Event | Alert | Incident |
|---|---|---|---|
| Meaning | **Something happened** (fact) | **A detected condition needs attention** (judgement by a rule) | **A related operational problem/case** a person works on |
| Table | `event_records` | `alerts` | `incidents` |
| Mutable? | No (append-only) | Status/ack/resolution fields only | Status fields, title, severity, time range |
| Created by | Device/connector, or Core engine (source `core-engine`) | Alert engine (`pipeline.detect`) or manual `POST /alerts` | Incident engine (`assign_incidents`) |
| Lifecycle | none | `open → acknowledged → resolved` **[Existing]** | `open → acknowledged → resolved` (resolved final) **[Existing]** |
| Severity | as reported (`info`/`warning`/`critical`) | engine-assigned | max of its alerts |

Existing behaviour retained: an alert belongs to at most one incident (`alerts.incident_id`); grouping rule `same_asset_or_system_within_15_min` (`incident_engine.py`); incident status changes cascade to alerts. Note [Existing]: the dashboard KPI “Alerts raised” counts **events** with warning/critical severity (`dashboard_core_pg._alert_counts`); the canonical vocabulary calls that “events with severity” (see `CORE_DATA_ARCHITECTURE.md` F-10).

---

## 3. Entity relationships

![Entity relationships](./img/Entity_relationships.png)

Diagram notes: `retention_policies`, `unit_conversions`, `metric_type_system_compat`, `event_type_system_compat` are catalogue/config tables without drawn relationships to keep the diagram readable. `building_id` columns marked “NEW column” are denormalised and kept consistent by composite FKs (§15). `asset_external_ids` has composite key `(source_id, external_id)`.

---

## 4. Building and location hierarchy

**Ownership & isolation model** [Proposed]

- `organizations (1) → buildings (n)`: supports several buildings per customer and several customers later. A single-customer install has one `organizations` row (`organization_code='DEFAULT'`), so no behaviour changes.
- Isolation unit = `building_id`. Every operational row carries it. Queries accept an optional building scope; permissions (future) are granted per building.
- **Sites/campuses** are **not** modelled as a separate table. A campus is represented by `locations` with `location_type='SITE'` under a building *or* by grouping buildings under an organization. **[Open Q5]**.
- **Relationship with the existing implementation:** `buildings` and `locations(building_id)` already exist; the KPI API already filters by `building_id` (`dashboard_core_pg.SCOPE_CTE`). The change is mostly *making the building explicit* and *extending the filter to the other endpoints*.

**`buildings` [Existing] + new columns**

| Column | Notes |
|---|---|
| `building_id` BIGINT PK | [Existing] |
| `name` TEXT UNIQUE | [Existing] (should become unique per organization **[Open]**) |
| `organization_id` BIGINT FK | **[NEW column]**, backfilled to the default organization |
| `building_code` TEXT UNIQUE | **[NEW column]** stable code used in the contract (e.g. `MAIN-TOWER`) |
| `timezone` TEXT NULL | **[Implemented column, 009]** IANA name; CLI validates with Python `zoneinfo`; existing buildings are backfilled to `Etc/GMT-2`; NULL uses the approved compatibility default in future application code |
| `work_start_local`, `work_end_local` TIME NULL | **[Implemented columns, 009]** checked start-before-end; existing buildings backfilled to 08:00–18:00; alert behavior replacement is future work |
| `non_working_weekdays` SMALLINT[] NULL | **[Implemented column, 009]** ISO weekday values 1–7; existing buildings backfilled to an empty array; holiday dates are deferred |

**`locations` [Existing] + new columns**

| Column | Notes |
|---|---|
| `location_id`, `building_id`, `floor`, `name` | [Existing]; UNIQUE `(building_id, name)` stays |
| `parent_location_id` BIGINT FK (self) | **[NEW column]** hierarchy: SITE → FLOOR → ZONE → ROOM; NULL = top level |
| `location_type` TEXT | **[NEW column]** `SITE\|FLOOR\|ZONE\|ROOM` (existing rows → `ZONE`) |
| `zone_type` TEXT, `is_sensitive` BOOLEAN | **[NEW column]** replace the string match `zone == "Server Room"` (`alert_engine.py`) |

Constraint: a child location must belong to the same building as its parent (composite FK `(parent_location_id, building_id) → locations(location_id, building_id)`).

---

## 5. Systems and assets

**`system_types` [Existing]** — canonical codes are the stored codes: `HVAC`, `NETWORKING`, `ACCESS_CONTROL`, `CCTV`, `FIRE_ALARM`. The `CHECK (code IN (...))` is to be dropped so a new system is an `INSERT`. UI/engine names (`NETWORK`, `ACCESS`) remain a presentation mapping at the API edge (AD-04).

**`assets` [Existing] + new columns**

| Column | Status | Description |
|---|---|---|
| `asset_id` | Existing | Internal ID (BIGINT identity). The only ID other tables reference. |
| `asset_code` | Existing | Stable human code, globally UNIQUE today (e.g. `HVAC-CRAC-01`). Used in the contract as the *internal* identifier. |
| `name`, `asset_type`, `system_type_id`, `location_id` | Existing | |
| `building_id` | NEW column | Denormalised; must equal the location’s building (composite FK). |
| `parent_asset_id` | NEW column | Parent-child equipment (e.g. fan coil → AHU), same building. Also usable later by incident correlation (`incident_engine.py` notes that no dependency table exists). |
| `manufacturer`, `model`, `serial_number` | NEW column | Descriptive only; **no logic may branch on them**. |
| `lifecycle_status` | NEW column | `PLANNED\|ACTIVE\|MAINTENANCE\|DECOMMISSIONED`. This is *installation/operational lifecycle*, not equipment state (§8). |
| `installed_at` | NEW column | |
| `metadata` JSONB | NEW column | Opaque descriptive data. |

**`asset_external_ids` [NEW entity]** — maps a vendor’s identifier to the internal asset.

| Column | Notes |
|---|---|
| `source_id` FK → `data_sources` | The vendor/connector namespace |
| `external_id` TEXT | Vendor asset/sensor identifier |
| `asset_id` FK → `assets` | |
| PK `(source_id, external_id)` | One vendor ID resolves to exactly one asset per source |

An asset may have several external IDs (several sources). The contract may identify an asset by `asset_code` **or** by `external_id` (resolved through the sending source).

---

## 6. Telemetry measurement contract (storage view)

`telemetry_records` [Existing] keeps `telemetry_id`, `asset_id`, `metric_id`, `ts`, `value` and `UNIQUE (asset_id, metric_id, ts)`.

| Column | Status | Description |
|---|---|---|
| `telemetry_id` | Existing | Measurement ID |
| `building_id` | NEW column | See §4 |
| `asset_id`, `metric_id` | Existing | |
| `ts` | Existing | **Observed time** (UTC) |
| `received_at` | NEW column | **Platform receive time.** NULL = unknown (legacy rows). Never back-filled with a guess. |
| `value` | Existing | `DOUBLE PRECISION NOT NULL`, finite (existing CHECK) |
| `source_id`, `batch_id` | NEW column | Provenance; legacy rows → source `legacy-seed` |
| `quality` | NEW column | `GOOD\|UNCERTAIN\|BAD`, default `GOOD` for legacy rows (§13) |
| `contract_version` | NEW column | e.g. `1.0` |
| `source_metadata` | NEW column | Optional JSONB: raw payload or reference to it |

**Unit normalization** — *connectors convert, Core validates.* The contract’s `unit` must equal `metric_types.unit` (the canonical unit) or a unit registered in `unit_conversions` for that metric (Core then converts and records the original in `source_metadata.original_value/original_unit`). Unknown unit → reject (`UNIT_MISMATCH`). For the current runtime, `bandwidth_util` is canonical `%`; its alert thresholds are provisional utilization percentages because the model has no per-link capacity for Mbps conversion.

**Timestamp normalization** — `observed_at` must be RFC 3339 UTC with `Z` (fractional seconds allowed). Offsets other than `Z` are rejected, not silently converted (the existing KPI parser, `dashboard_core_pg.parse_utc_timestamp`, applies the same strictness to query parameters). Future tolerance: `observed_at ≤ received_at + 5 min` (the rule already used by `POST /events`, `FUTURE_TOLERANCE`). Core never overwrites `observed_at`.

**Validation (in order):** schema → known building/source → asset resolution → metric known → metric allowed for the asset’s system (`metric_type_system_compat`) → unit → timestamp → finite value → plausible range (`min_plausible/max_plausible`; violation ⇒ stored as `BAD`, not rejected).

**Duplicate handling & idempotency** — natural key `(asset_id, metric_id, ts)` (existing UNIQUE):
- same key, same value → success, counted as `duplicate` (idempotent retry);
- same key, different value → **conflict**: first write wins, new record goes to `ingestion_rejections` with `reason_code='CONFLICT'` for review. **[Open]** whether a source may legitimately *correct* a value.

---

## 7. Engineering units

Canonical unit = `metric_types.unit` **[Existing]**. Current seed values [Verified]:

| metric (`name`) | canonical unit today | Note |
|---|---|---|
| `temperature` | `C` | engine labels it `°C` (presentation) |
| `humidity` | `%` | |
| `power_kw` | `kW` | |
| `cpu_load` | `%` | |
| `packet_loss` | `%` | |
| `bandwidth_util` | `%` | Alert thresholds currently use provisional utilization percentages (80% warning, 90% critical mean); no per-link capacity is stored for Mbps conversion. |
| `smoke_level` | `%/m` | |
| `camera_fps` | `fps` | |

Migration 008 implements nullable `metric_types.min_plausible` and `max_plausible`; values outside configured bounds are retained with `quality=BAD` and excluded from operational readers. `value_kind` (`NUMERIC|BOOLEAN|COUNTER`) and `unit_conversions(from_unit, to_unit, factor, offset)` remain proposals; connectors remain the primary unit converters.

---

## 8. Equipment state model

Four *independent* dimensions plus lifecycle. All are about the **equipment/data path**, none is derived from “no data” alone.

| Dimension | Values | Derived from |
|---|---|---|
| `operational_state` | `NORMAL \| WARNING \| CRITICAL \| UNKNOWN` | Open fault conditions and event severities. Reuses the existing rule: critical = latest event of an opener/closer pair is the opener with severity critical, or an unpaired critical event in the window; warning = warning event(s) in the window; `UNKNOWN` = asset has never produced any telemetry/event. |
| `availability` | `AVAILABLE \| UNAVAILABLE \| UNKNOWN` | **Core event evidence only** in the G3 shadow projection (`camera_offline`/`link_down` and their recovery events). Connector-reported availability remains evidence and never sets Core availability. Silence never sets `UNAVAILABLE`. |
| `communication_status` | `COMMUNICATING \| DEGRADED \| SILENT \| UNKNOWN` | The *data path*, derived from per-series freshness for an unambiguous single-source asset; multiple sources or mixed sourced/unattributed series produce `UNKNOWN`. Source liveness (`ALIVE`/`SILENT`) remains separately visible. Silence is evidence and never means equipment `OFFLINE`/`UNAVAILABLE`. |
| `freshness` | `FRESH \| STALE \| MISSING \| NOT_APPLICABLE` | Age of last observation vs expected interval (§9). |
| `lifecycle_status` (on `assets`) | `PLANNED \| ACTIVE \| MAINTENANCE \| DECOMMISSIONED` | Provisioning; assets not `ACTIVE` are excluded from freshness alarms. |

**`asset_state_current`** [Implemented additively in migration 012, shadow only] — one row per asset with `operational_state`, `availability`, `communication_status`, `evaluated_at`, `state_since`, a reason, JSONB `evidence_summary`, and `evidence_schema_version`. Existing readers remain authoritative.
**`asset_state_history`** [Implemented additively in migration 012] — append-only baseline on first evaluation, then one row only when the semantic tuple (`operational_state`, `availability`, `communication_status`) changes. Freshness-only changes stay in `series_freshness_current` and current evidence. A database trigger rejects update/delete; the G5 retention policy is still an explicit decision.

**Compatibility mapping (shadow only):** `critical`→`operational_state=CRITICAL`; `warning`→`WARNING`; `normal`→`NORMAL`; `no_data` and `unknown`→`UNKNOWN`. This mapping does not replace or feed the current KPI/dashboard/alert/chat readers. See [`ASSET_STATE_DESIGN.md`](ASSET_STATE_DESIGN.md) for the full dimension mapping and parity evidence.

---

## 9. Freshness and missing-data semantics

**`reporting_expectations`** [Implemented schema, migration 009]: scopes are exactly `ASSET_METRIC`, `ASSET_TYPE_METRIC`, `SOURCE`, and `GLOBAL`, resolved in contract §9 order. Fields include `record_kind`, metric/event key, positive interval, stale/missing multipliers, priority, validity range, and expectation version. Seeded global rows are telemetry 900s, heartbeat 3600s, and source keepalive 300s; series stale/missing multipliers are 2/5. The freshness job reads these expectations. Event-only devices without an expected heartbeat remain `NOT_APPLICABLE` under contract §9.

**`data_gaps` and `series_freshness_current`** [Implemented schema, migrations 009–010]: the freshness job writes current series states and opens a gap only at `MISSING`; `gap_start` is the last valid observation time and `detected_at` is separate. A gap closes only on a valid observation newer than the last valid observation at open time. Back-dated records neither close nor split it; `BAD` never closes it. Gaps do not create alerts or change asset health. Source liveness remains independent evidence under contract §9 and migration 011.

**Series freshness rules** (implemented by the E3 evaluator; thresholds are configurable):

- `age = evaluation_time − last_observed_at` (data age).
- `FRESH` if `age ≤ stale_factor × E` (default `stale_factor = 2`);
- `STALE` if `stale_factor × E < age ≤ missing_factor × E` (default `missing_factor = 5`);
- `MISSING` if `age > missing_factor × E`;
- `NOT_APPLICABLE` when no expectation exists or `lifecycle_status ≠ ACTIVE`; never `FRESH` without at least one observation (`MISSING`/`UNKNOWN` instead).
- Series freshness is evaluated per expected series. G3 does not collapse the series list into one freshness state; its separate communication projection follows the mapping in §8.
- `evaluation_time` = wall clock in `LIVE` mode; an explicit `as_of` in `REPLAY` mode (keeps today’s data-relative demo behaviour, AD-07).
- Source liveness uses the E4b states `ALIVE`, `SILENT`, `UNKNOWN`, and `DISABLED`; `SILENT` is based on the configured 3× keepalive threshold. It does not itself set asset `OFFLINE`/`UNAVAILABLE`, change asset health, or create alerts. The G3 communication projection separately maps series freshness and source attribution as described in §8.

**Missing vs delayed vs irregular:** missing = no row within the window → `data_gaps` row; delayed = row exists but `received_at − ts` exceeds a per-source threshold (derived flag, not stored); irregular = observed spacing deviates from `E` (reported by the gap query, `HISTORIAN_DESIGN.md` §9).

---

## 10. Events, alerts, and incidents

**`event_records` [Existing] + new columns:** `building_id`, `received_at`, `source_id`, `source_event_id` (connector’s own event ID; dedupe key), `contract_version`. `severity` stays the *reported* severity (`info|warning|critical`). `details` JSONB unchanged (existing consumers read keys such as `reason`, `badge`/`card_id`, `seconds_open`, `port`).
`event_types` gains `event_class` (`ACTIVITY|FAULT_OPEN|FAULT_CLOSE|HEARTBEAT|STATE_REPORT`) and `resolves_event_type_id` (self-FK; e.g. `link_up → link_down`), consolidating the three hand-written pair lists (`dashboard_core_pg.FAULT_RECOVERY_PAIRS`, `integrity_rules.CLOSERS`, `v_camera_status`).
**System-generated events** (e.g. `state_changed`, `source_status_changed`) are ordinary `event_records` whose `source_id` points to the built-in source `core-engine` (`source_type='CORE'`).
Idempotency: migration 007 preserves the legacy natural key `(asset_id, event_type_id, ts)` only when `source_event_id IS NULL`; connector events use `UNIQUE (source_id, source_event_id) WHERE source_event_id IS NOT NULL` so distinct events in the same second are not collapsed.

**`alerts` [Existing] + new columns:** `building_id`; `detected_at` (processing time — today’s `detected_ts` is the first data timestamp of the detection, F-16); `acknowledged_ts`, `acknowledged_by`, `resolved_by`; `rule_version`. Threshold vs anomaly vs compound stays in `detection_method`. Add `UNIQUE (rule_id, asset_id, first_ts)` (F-14). Lifecycle remains `open → acknowledged → resolved` with `CHECK ((status='resolved') = (resolved_ts IS NOT NULL))` (existing).
**`incidents` [Existing] + new column** `building_id` (derived from `location_id`); lifecycle and grouping unchanged.

---

## 11. Evidence model

Existing mechanism [Verified]: `alerts.evidence` JSONB holds `{thresholds, facts, event_count, events:[{ref:"E1", source:"event|telemetry", record_id, ts, event_type, value, unit, details}]}`; `evidence.py` re-reads each row from `event_records`/`telemetry_records` and compares timestamp/value/event type.

**`alert_evidence` [NEW entity]** (normalised mirror, dual-written; JSON stays for compatibility):

| Column | Description |
|---|---|
| `evidence_id` PK, `alert_id` FK, `ref` (`E1…`) | Same refs as the JSON; UNIQUE `(alert_id, ref)` |
| `evidence_type` | `MEASUREMENT \| EVENT \| STATE_CHANGE` |
| exactly one of `telemetry_id`, `event_id`, `state_id` | FKs to the source row; `CHECK` exactly-one |
| `asset_id`, `building_id` | Copied from the source row; must match the alert’s asset/building |
| `observed_at`, `received_at` | Copied from the source row |
| `source_id` | Copied from the source row |
| `validation_status` | `UNVERIFIED \| VERIFIED \| MISMATCH \| SOURCE_MISSING` — maps to `evidence._verify` outcomes: none→`VERIFIED`; “row not found”→`SOURCE_MISSING`; timestamp/value/event-type differs→`MISMATCH` |
| `validated_at` | |
| `snapshot` JSONB | Verbatim copy of the evidence item at detection time |
| `metadata` JSONB | |

Evidence remains traceable to actual data: AI text is only ever checked **against** these rows (`validation.py` grounding checks) and is never itself an evidence source. Computed facts (`evidence.facts`, e.g. baseline median) stay in `alerts.evidence.facts`; they are not row-backed and are labelled as derived.

---

## 12. Data source model

**`data_sources`** [NEW entity]

| Column | Description |
|---|---|
| `source_id` PK, `source_code` UNIQUE | e.g. `legacy-seed`, `core-engine`, `vendor-a-bms-main-tower` |
| `building_id` FK | A connector serving two buildings registers two sources |
| `source_type` | `CONNECTOR \| SIMULATOR \| MANUAL \| API \| CORE` |
| `connector_name`, `connector_version`, `vendor`, `integration_name` | Identity (descriptive only) |
| `status` | `ACTIVE \| DEGRADED \| ERROR \| DISABLED` |
| `last_success_at`, `last_error_at`, `last_error` | Updated by ingestion |
| `source_metadata` JSONB | Opaque |

Migration 006 implements `data_sources`, `source_credentials`, source-scoped `asset_external_ids`, `ingestion_batches`, `ingestion_rejections`, and `equipment_state_records`. It does not seed legacy/core sources or backfill existing fact provenance; existing rows remain without a source until separately provisioned.

---

## 13. Data quality indicators

| Indicator | Meaning | Stored how | Used by |
|---|---|---|---|
| `GOOD` | Valid, plausible, trusted | `telemetry_records.quality` | KPI, alerts, charts |
| `UNCERTAIN` | Valid but source flagged low confidence (e.g. sensor warming up, substituted value) | `quality` | KPI/alerts **include** by default but the evidence marks it; configurable |
| `BAD` | Structurally valid but unusable (outside plausible range, connector-declared bad) | `quality` | Kept for audit; **excluded** from KPI/alerts via a compatibility view |
| `MISSING` | No value was obtained for an expected observation | Not accepted as telemetry quality; migration 009 adds `data_gaps`, but no evaluator writes to it yet | future freshness/gap reports |

How other conditions are represented:

| Condition | Representation |
|---|---|
| **Invalid** (schema, unknown asset/metric/unit, bad timestamp, non-finite value) | Rejected; row in `ingestion_rejections`; never in the Historian |
| **Implausible** (outside `min/max_plausible`) | Stored `BAD` |
| **Stale** | Derived `freshness=STALE` (not a property of a measurement) |
| **Missing** | Absence; future evaluator opens/closes the migration-009 gap audit |
| **Delayed** | `received_at − ts > source delay threshold`; derived `is_late`; late rows can still be evaluated by the idempotent detection run |
| **Duplicate** | No-op, counted in `ingestion_batches.duplicate_count` |

Existing rows retain `quality=NULL` because their historical quality is unknown. `v_telemetry_valid` includes NULL and excludes only `BAD`, preserving legacy readings in operational calculations.

---

## 14. Primary and foreign key strategy

- **Surrogate keys:** `BIGINT GENERATED … AS IDENTITY`, named `<entity>_id` (existing convention). Only surrogate keys are referenced by FKs.
- **Stable codes** (immutable once referenced): `organization_code`, `building_code`, `asset_code`, `source_code`, `metric_types.name`, `event_types.name`, `system_types.code`. These are what connectors use; they are resolved to surrogate keys at ingestion.
- **Natural keys for idempotency:** telemetry `(asset_id, metric_id, ts)`; events `(source_id, source_event_id)` plus legacy `(asset_id, event_type_id, ts)`; alerts `(rule_id, asset_id, first_ts)`; external IDs `(source_id, external_id)`.
- **Building consistency** via composite FKs: `locations(location_id, building_id)` and `assets(asset_id, building_id)` become UNIQUE targets; child tables reference `(asset_id, building_id)`.
- **Delete policy:** `ON DELETE RESTRICT` everywhere; retirement is `lifecycle_status='DECOMMISSIONED'`, not deletion.
- **Partition note:** if `telemetry_records` is later partitioned by `ts`, its PK and any referencing FKs must include `ts` (`HISTORIAN_DESIGN.md` §14); `alert_evidence.telemetry_id` is therefore designed to also store `telemetry_ts` so the FK can become `(telemetry_id, ts)`.

## 15. Required database constraints and indexes (specification)

| Table | Constraint / index | Reason |
|---|---|---|
| `buildings` | nullable `timezone`, nullable work times, weekday range check, start-before-end check; existing rows receive parity defaults; CLI validates IANA names | migration 009; work-hour alert behavior is later |
| `locations` | `UNIQUE (location_id, building_id)`; FK `(parent_location_id, building_id)`; CHECK `location_type` | same-building hierarchy |
| `assets` | `UNIQUE (asset_id, building_id)`; FK `(location_id, building_id)`; FK `(parent_asset_id, building_id)`; `CHECK (parent_asset_id <> asset_id)`; CHECK `lifecycle_status` | building consistency, no self-parent |
| `asset_external_ids` | PK `(source_id, external_id)`; index `(asset_id)` | resolution |
| `data_sources` | `UNIQUE (source_code)`; CHECK `source_type`, `status`; index `(building_id)` | |
| `telemetry_records` | keep `UNIQUE (asset_id, metric_id, ts)`; FK `(asset_id, building_id)`; CHECK `quality`; `CHECK (received_at IS NULL OR ts <= received_at + interval '5 minutes')`; index `(building_id, ts)`; index `(source_id, received_at)` | idempotency, provenance, building analytics |
| `event_records` | keep source event unique key; legacy natural key is partial where `source_event_id IS NULL`; FK `(asset_id, building_id)` and additional indexes remain future proposals | F-15 |
| `event_types` | self-FK `resolves_event_type_id`; CHECK `event_class` | single fault/recovery definition |
| `reporting_expectations` | four scope values and exact scope-key CHECK; `UNIQUE NULLS NOT DISTINCT (scope, priority, scope keys)`; positive interval, `missing_multiplier > stale_multiplier`, validity check | migration 009; prevents equal-priority ambiguity |
| `data_gaps` | unique NULLS NOT DISTINCT series identity + `gap_start`; closure fields are all-null while open or all-set when closed | migration 009; backfill splitting deferred |
| `series_freshness_current` | unique NULLS NOT DISTINCT series identity; freshness/observation/age checks; FK to expectation and version used | migration 009; table is not populated until a future evaluator |
| `asset_state_current` | PK `asset_id`; CHECK enums; **CHECK: `freshness IN ('FRESH','NOT_APPLICABLE') OR operational_state` stays independent** (no silence-implies-state rule) | AD-06 |
| `asset_state_history` | index `(asset_id, observed_at DESC)`; append-only trigger | state history queries |
| `alerts` | `UNIQUE (rule_id, asset_id, first_ts)`; FK `(asset_id, building_id)`; index `(building_id, status, first_ts DESC)` | F-14 |
| `incidents` | index `(building_id, status, last_ts)` | |
| `alert_evidence` | `UNIQUE (alert_id, ref)`; exactly-one-source CHECK; FKs RESTRICT; index `(telemetry_id)`, `(event_id)` | referential evidence |
| `ingestion_batches` | `UNIQUE (source_id, batch_id)` | idempotent retries |

Append-only triggers (reject `UPDATE/DELETE`) on facts are **[Open]** — they conflict with any future retention job; deletion would run under a dedicated role/function.

---

## 16. JSON contract examples (contract version `1.0`) [Proposed]

These examples are a historical draft and are superseded for v1 by [`INGESTION_CONTRACT.md`](INGESTION_CONTRACT.md), which matches the implemented route.

### 16.1 Batch envelope (common)

```json
{
  "contract_version": "1.0",
  "batch_id": "b5c1a6a0-3c5e-4e0e-9a1e-2b7d8c1f0a11",
  "sent_at": "2026-11-15T09:15:04Z",
  "source": {
    "source_code": "vendor-a-bms-main-tower",
    "connector": { "name": "vendor-a-connector", "version": "0.3.1" }
  },
  "building_code": "MAIN-TOWER",
  "records": [ "…one or more record objects below…" ]
}
```

### 16.2 Normalized telemetry measurement

Two different vendors, same canonical output. Vendor A’s raw payload (Fahrenheit, epoch seconds) and Vendor B’s raw payload (Celsius string unit, local offset) are normalized **by their connectors**:

Vendor A raw (illustrative): `{"sensor":"TS-114","temp_f":76.28,"ts":1794734100}`
Vendor B raw (illustrative): `{"deviceId":"B-77/ai3","value":24.6,"unit":"Celsius","time":"2026-11-15T11:15:00+02:00"}`

```json
{
  "record_type": "telemetry",
  "asset": { "external_id": "TS-114" },
  "metric_code": "temperature",
  "value": 24.6,
  "unit": "C",
  "observed_at": "2026-11-15T09:15:00Z",
  "quality": "GOOD",
  "source_metadata": { "original_value": 76.28, "original_unit": "F", "raw_ref": "vendor-a/2026-11-15/0915" }
}
```

```json
{
  "record_type": "telemetry",
  "asset": { "external_id": "B-77/ai3" },
  "metric_code": "temperature",
  "value": 24.6,
  "unit": "C",
  "observed_at": "2026-11-15T09:15:00Z",
  "quality": "GOOD",
  "source_metadata": { "original_unit": "Celsius", "original_time": "2026-11-15T11:15:00+02:00" }
}
```

### 16.3 Equipment state update

```json
{
  "record_type": "state",
  "asset": { "asset_code": "CCTV-CAM-02" },
  "observed_at": "2026-11-15T09:20:11Z",
  "availability": "UNAVAILABLE",
  "communication_status": "COMMUNICATING",
  "reason_code": "VIDEO_SIGNAL_LOST",
  "message": "no video signal",
  "source_metadata": { "vendor_state": "NO_SIGNAL" }
}
```

### 16.4 Operational event

```json
{
  "record_type": "event",
  "source_event_id": "evt-000184233",
  "asset": { "asset_code": "ACS-RDR-02" },
  "event_type": "door_forced",
  "observed_at": "2026-11-15T09:21:47Z",
  "severity": "CRITICAL",
  "details": { "note": "door forced" }
}
```

### 16.5 Batch response (proposed)

```json
{
  "batch_id": "b5c1a6a0-3c5e-4e0e-9a1e-2b7d8c1f0a11",
  "received_at": "2026-11-15T09:15:05Z",
  "accepted": 1,
  "duplicates": 0,
  "rejected": [ { "index": 1, "reason_code": "UNIT_MISMATCH", "message": "unit 'F' is not allowed for metric 'temperature'" } ]
}
```

### 16.6 Field requirements

| Field | Telemetry | State | Event | Notes |
|---|---|---|---|---|
| `contract_version` (envelope) | **required** | required | required | `MAJOR.MINOR` |
| `source.source_code` | required | required | required | must exist in `data_sources` and match the source authenticated by its per-source API key |
| `building_code` | required | required | required | |
| `batch_id` | required (UUID) | required | required | retry idempotency |
| `record_type` | required | required | required | `telemetry\|state\|event` |
| `asset.asset_code` **or** `asset.external_id` | exactly one | exactly one | exactly one | conditional |
| `metric_code` | required | — | — | |
| `value` | required | — | — | finite number; a missing measurement is omitted, not sent as a quality value |
| `unit` | required with `value` | — | optional | must be canonical/registered |
| `observed_at` | required | required | required | UTC `Z` |
| `quality` | optional (default `GOOD`) | — | — | `GOOD\|UNCERTAIN\|BAD`; `MISSING` is invalid |
| `event_type` | — | — | required | in `event_types`, valid for the asset’s system |
| `severity` | — | — | optional (default `INFO`) | `INFO\|WARNING\|CRITICAL` (uppercase in contract; stored lowercase as today) |
| `source_event_id` | — | — | required | event identity key |
| `operational_state`, `availability`, `communication_status` | — | at least one required | — | enums §8 |
| `reason_code`, `message`, `details`, `source_metadata` | optional | optional | optional | opaque JSON objects; size-limited **[Open]** |

`received_at` is **never** supplied by the sender; the Core stamps it.

---

## 17. Versioning and compatibility strategy

- **Contract version** `MAJOR.MINOR` in every envelope and stored per row (`contract_version`). This implementation accepts `1.0`; future compatibility/deprecation windows require a versioned contract change.
- **Unknown fields:** ignored with a warning in the response; only `source_metadata`/`details` content is persisted opaque. Unknown enum values → reject.
- **Schema evolution:** migrations `003_incidents.sql` through `010_data_gap_lifecycle.sql` exist. Migration 006 adds `schema_migrations`; `scripts/apply_migrations.py` applies 006+ to existing databases under an advisory lock. Compose init mounts still apply only to empty database volumes.
- **Backward compatibility rules:** migrations 006–010 preserve historical telemetry/event/alert/incident facts; migration 009 backfills building schedule fields and 010 enriches pre-existing gap audit rows without changing fact records. Existing endpoints and response shapes (`/dashboard/*`, `/alerts`, `/incidents`, `/chat`, `/ask`, `POST /events`, `POST /alerts`) remain supported. Legacy facts retain NULL source/quality/received metadata, and legacy event duplicate handling stays intact.
- **API compatibility:** the versioned route is `POST /api/v1/ingest/batches`; batch result retrieval is `GET /api/v1/ingest/batches/{batch_id}`.
- **Terminology for UI vs DB** stays a translation layer (AD-04).
