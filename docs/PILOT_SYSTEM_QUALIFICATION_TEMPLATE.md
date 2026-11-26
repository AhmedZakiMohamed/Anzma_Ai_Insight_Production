# Anzma AI Insight Pilot System Qualification

**Status:** Fillable discovery template; no vendor, device, network, or deployment support is implied until this record is completed and reviewed.  
**Related:** [`WHOLE_SOLUTION_ARCHITECTURE.md`](WHOLE_SOLUTION_ARCHITECTURE.md), [`CONNECTOR_ARCHITECTURE.md`](CONNECTOR_ARCHITECTURE.md), [`INGESTION_CONTRACT.md`](INGESTION_CONTRACT.md), [`ARCHITECTURE_DECISION_REGISTER.md`](ARCHITECTURE_DECISION_REGISTER.md)

Use this worksheet with the customer/site operator and source-system owner to choose and qualify the first real pilot integration. Do not put passwords, API keys, certificates, personal data or production payloads in this document or Git. Record only a secret-manager reference and sanitized sample metadata here.

## 1. Pilot identity and ownership

| Field | Value / notes |
|---|---|
| Customer / business unit |  |
| Site / building code |  |
| Building timezone (IANA) |  |
| Site type and operating context |  |
| Pilot objective / measurable business outcome |  |
| Product owner / decision approver |  |
| Customer site IT/network contact |  |
| Vendor/source-system contact |  |
| Anzma integration owner |  |
| Anzma data/analytics reviewer |  |
| Independent QA reviewer |  |
| Deployment model decision (ADR-01) | Open / on-prem-private / SaaS / both |
| Pilot start, review, and exit dates |  |

## 2. Source system and device inventory

Complete one row per distinct source product/version and one inventory export per meaningful device/point family.

| Field | Required details |
|---|---|
| System family | BMS/HVAC, access control, VMS, meters, IoT, CMMS/ERP, other |
| Vendor and product | Manufacturer, product name, edition/license |
| Version | Software version, API/SDK version, relevant firmware range |
| Deployment | On-prem server, controller, appliance, hosted/cloud API, protocol gateway |
| Interface | REST/GraphQL, SDK, BACnet/IP, BACnet/SC, OPC UA, MQTT, file/export, other; mark what is actually supported/licensed |
| Base endpoint / gateway | Sanitized hostname or network zone; no credentials |
| Authentication mode | OAuth/client credentials, certificate, scoped API key, service account, etc. |
| Secret reference | Secret manager path/ID only; never the secret itself |
| Permission scope | Exact read permissions; confirm no write/control permission is granted |
| Devices/assets | Count, types, hierarchy and stable external identifier format |
| Points/signals | Count by metric/type/unit/sample cadence/quality where available |
| Events | Event types and source definitions; distinguish decisions, physical transitions, alarms and recovery |
| History | Available depth, archival source, query maximum, retention and expected backfill size |
| Current-state support | Yes/no, supported objects, timestamp/refresh semantics |
| Live notification support | Polling, webhook, subscription, message broker, none; latency and cursor support |
| Rate/concurrency limits | Vendor documented limits and any license restrictions |
| Clock/time | Timezone, UTC support, NTP status, clock skew, timestamp precision |
| Data quality | Quality/status codes, null/sentinel values, unit conventions, known gaps |
| Privacy classification | Personal/sensitive fields present? purpose, access, retention approval |
| Compatibility evidence | Vendor docs, sandbox access, license confirmation, test account, sanitized fixture |

### Device / point inventory sample row

| source external ID | parent/hierarchy | source label | proposed Core asset/metric/event | type/unit | cadence or event semantics | quality/status | mapping confidence | approved by |
|---|---|---|---|---|---|---|---|---|
|  |  |  |  |  |  |  |  |  |

Add rows in the customer's secured inventory system or a protected workbook when the list contains sensitive identifiers. The checked-in copy must contain synthetic or sanitized identifiers only.

## 3. Capability qualification

For each item select **Verified**, **Unavailable**, or **Not tested**. Attach sanitized evidence reference and connector version. “Empty response” is not evidence that a capability is unavailable; confirm through vendor documentation/configuration or a controlled test.

| Capability | Status | Scope/limits/evidence reference |
|---|---|---|
| Test connection and report permissions |  |  |
| Discover systems/devices/assets |  |  |
| Discover points/tags/units |  |  |
| Read historical telemetry |  |  |
| Read current telemetry/state |  |  |
| Read historical events/alarms |  |  |
| Receive live events or notifications |  |  |
| Read online/offline transitions |  |  |
| Read physical door contact/open events |  |  |
| Read camera recording-health metadata |  |  |
| Read energy meter intervals |  |  |
| Read maintenance/work-order records |  |  |
| Enumerate paging and completion reliably |  |  |
| Detect source outage and recover checkpoint |  |  |

Do not mark every capability “Verified” just because the connector manifest lists it. Each listed capability must have a concrete proof case and source-specific limit.

## 4. Network and security

| Question | Answer / evidence |
|---|---|
| Where does the source live (network zone/site/cloud)? |  |
| Where will the connector process run? |  |
| Where will Anzma Core run? |  |
| Which side initiates connections? |  |
| Required source/Core DNS names and ports |  |
| TLS mode, trust chain, certificate owner/rotation |  |
| Firewall/proxy/allow-list changes and approver |  |
| Is outbound connector-to-Core HTTPS permitted? |  |
| Vendor API host allow-list / SSRF control |  |
| Read-only account provisioned and tested? |  |
| Credential rotation/revocation procedure |  |
| Source and operator identities kept separate? |  |
| User identity provider and role/scope model |  |
| Personal data present? Approved purpose/access? |  |
| Audit/log retention and redaction requirements |  |

Store secrets in an approved secret manager. Never expose them to model prompts, browser local storage, source control, error messages or logs.

## 5. Data mapping and analytic readiness

| Analytic / question | Required records and semantics | Ready / partial / unavailable | Missing prerequisites / limitation | Business/data approver |
|---|---|---|---|---|
| After-hours physical door openings | Door-open/contact events, stable door mapping, building timezone, history coverage |  |  |  |
| Device offline frequency/duration | Explicit offline/recovery transitions or approved state history; deduplication and coverage |  |  |  |
| Maintenance candidates | Fault history, durations, valid condition trends, asset context; approved transparent score |  |  |  |
| Energy anomaly | Meter unit/interval, quality, comparable baseline and required load/time context |  |  |  |
| Operational alert explanation | Current incident/alert lifecycle, validated evidence and freshness |  |  |  |
| Other pilot question |  |  |  |  |

For each analytic, attach sanitized raw-source examples, the normalized record mapping, expected result, SQL/reference calculation or manual reconciliation, evidence links, data-gap case, timezone boundary and permission-negative case.

## 6. Workload and infrastructure sizing inputs

Capture measured or vendor-documented values; label estimates and their source. Do not convert a guess into a supported deployment tier.

For this project, the current sizing hypothesis is 3,000 total points at a 5-minute cadence, 90 days online, 2 buildings, 5 system types, 248 assets, 10 concurrent users, and 2–4 simultaneous AI requests using a 7–8B Q4 bilingual model. These are **planning assumptions only**: the 248-asset count and five-role model are not current prototype facts, and the workload has not been benchmarked. See [`WHOLE_SOLUTION_ARCHITECTURE.md` §11](WHOLE_SOLUTION_ARCHITECTURE.md#working-planning-profile-for-scenario-comparison) for the scenario tiers and repository checks. Replace the hypothesis below with customer-specific values and evidence.

| Input | Value | Measured / estimated / vendor-documented | Source / confidence |
|---|---:|---|---|
| Buildings and locations |  |  |  |
| Devices/assets |  |  |  |
| Points/signals (record whether count is per building, site, or portfolio) |  |  |  |
| Average samples per point per day |  |  |  |
| Peak aggregate ingestion records/sec |  |  |  |
| Events per day and burst size |  |  |  |
| Initial history/backfill records |  |  |  |
| Average serialized bytes per normalized record (measured) |  |  |  |
| Required online history retention |  |  |  |
| Backup retention and destination |  |  |  |
| Concurrent users and dashboards |  |  |  |
| User roles, permissions and resource scope (role names/count alone are insufficient) |  |  |  |
| Analytics/report queries per hour |  |  |  |
| Alert delivery target and tolerated outage |  |  |  |
| Connector outage/buffer duration |  |  |  |
| Local model required? model artifact, quantization, context, output limit and peak concurrent requests |  |  |  |
| GPU model/VRAM and inference runtime (if used) |  |  |  |
| Core host CPU/RAM/storage and gateway CPU/RAM/storage/network ports |  |  |  |
| Expected report/artifact generation and retention |  |  |  |

Calculate capacity from measured records/day, bytes/record, indexes/overhead, WAL, backups, retention and growth reserve. Measure actual database growth and ingest/query latency under representative backfill and live load. Benchmark the Core/database with and without local inference when local AI is required. Record CPU, memory, storage/IOPS, network and restore behavior at each test point.

## 7. Qualification and acceptance evidence

| Test | Expected evidence | Result / artifact reference |
|---|---|---|
| Declared workload and point-count scope | Confirm total points per site/building; reconcile expected sample count for cadence and retention |  |
| Least-privilege connection | Read succeeds; write/control is denied or absent |  |
| Capability discovery | Actual exposed objects listed; unsupported distinct from empty |  |
| Mapping review | Correct, uncertain, conflicting and unmapped cases handled visibly |  |
| Pagination and history | All expected pages accounted for; no silent gaps |  |
| Retry/restart | Duplicate delivery idempotent; checkpoint/recovery behavior verified |  |
| Time and quality | UTC/local conversion, clock skew, units and quality mapping reconciled |  |
| Analytic reconciliation | Result matches sanitized source fixtures and approved calculation |  |
| Incomplete source history | Result says partial/unknown rather than asserting no events |  |
| Authorization and privacy | Forbidden building/data/person scope denied and audited |  |
| Representative telemetry load | Ingestion, query, database/index/WAL growth, storage and resource measurements recorded against the agreed cadence/retention profile |  |
| AI CPU/GPU comparison | Fixed Arabic/English question set at 1/2/4 concurrent requests; record response quality, p50/p95 latency, peak system RAM/VRAM, and deterministic fallback behavior |  |
| Role enforcement | Each approved role is denied out-of-scope buildings, systems, data categories, actions and exports |  |
| Deployment and recovery | Install, restart, upgrade, backup and restore evidence recorded |  |
| Independent review | Reviewer repeats the critical proof cases on named release revision |  |

## 8. Go/no-go record

| Field | Value |
|---|---|
| Connector ID/version and manifest hash |  |
| Core release/commit and migration version |  |
| Source vendor/version and environment |  |
| Known supported capabilities |  |
| Known exclusions and customer prerequisites |  |
| Open severity-1/2 risks |  |
| Customer IT/security approval |  |
| Data/business owner approval |  |
| Anzma engineering approval |  |
| Independent QA decision | Pass / Conditional / Fail |
| Date and evidence bundle location |  |

Only the verified capabilities on this record may be described as supported for this source/version. A synthetic reference service proves connector mechanics, not real-vendor compatibility.
