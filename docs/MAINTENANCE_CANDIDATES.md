# `maintenance.candidates`

Status: **implemented as a deterministic proposal; policy approval is pending.** This is an inspection-priority aid. It
does not predict failure, diagnose an asset, create a work order, or authorize physical actions.

## Data and readiness assessment

The tool reads canonical `assets`, `locations`, `buildings`, `system_types`, `event_types`, `event_records`,
`metric_types`, `v_telemetry_valid`, and source identity records. Its facts are scoped to one building and a maximum
90-day analysis window. It does not read alerts or incidents as independent faults: repeated alert delivery therefore
cannot increase the recurrence factor. A genuine fault is one stored event whose migration-014 catalogue fields are
`event_class='FAULT_OPEN'` and `outage_class='FAULT'` (for example, `hvac_fault` or `fire_alarm_triggered`). The
availability event catalogue is kept separate from this fault classification.

Condition signals are limited to existing threshold-backed metrics:

| System | Metric | Required unit | Warning / critical source |
|---|---|---|---|
| HVAC | `temperature` | `C` | `T.hvac_temp_warn` / `T.hvac_temp_crit`; `Server Room` uses the existing `T.server_temp_warn` / `T.server_temp_crit` rule. |
| NETWORKING | `bandwidth_util` | `%` | `T.bandwidth_warn_pct` / `T.bandwidth_crit_mean_pct`. |
| NETWORKING | `packet_loss` | `%` | `T.packet_loss_warn` / `T.packet_loss_crit_mean`. |

These are the current `app/domain/alerts/alert_config.py` / `alert_engine.py` thresholds, including existing
provisional thresholds. No unit conversion is attempted. For each asset, the highest normalized supported reading in
the requested period is selected. This is a threshold-excursion signal, **not a fitted time-series slope or a claim of
degradation**. A reading at or below warning scores zero; a value just above warning starts at 50; the score increases
linearly to 100 at critical and is capped above it. A measurement above warning is a candidate signal. A supported
measurement below warning is only used as a zero condition factor when another signal has already made the asset a
candidate. No reading means unknown, not zero.

The repository contains no maintenance history, inspection history, last-service date, equipment age/service life, or
work-order table. That context is emitted as unavailable and has no scoring weight in this proposal. CCTV camera FPS,
fire smoke level, access-control activity, and other metrics have no approved condition-priority thresholds here and
are not compared as condition factors. This tool does not assume that a source's silence or a missing reading means
healthy equipment.

### G1 / G2 status

`docs/CONNECTOR_ARCHITECTURE.md` and `connectors/reference_simulator/` describe a separate reference simulator. Its
`seed:` external IDs and seeded values are demo fixtures, not proof of a real vendor connector. The repository does not
currently prove production vendor connection, full source-history retrieval, or universal discovery/mapping. The tool
uses existing canonical `asset_id` associations; it cannot repair unmapped vendor identifiers.

Completeness is supplied only by a trusted server-side `MaintenanceCoverageProvider`, not by a caller/model. It must
confirm event **and** telemetry history, and the exact start/end coverage, for each relevant source. No persisted
coverage ledger or configured production provider exists today. With the current default `coverage_provider=None`, the
envelope is PARTIAL and scores are withheld. A future runtime must supply verified coverage before presenting ranked
scores as complete. `DEFAULT_REGISTRY` is still empty, and no active Chat route calls the Tool Envelope guard; this
module is **not Chat-integrated**.

## Proposed score policy (not approved)

Policy identifier: `maintenance-priority-proposal-1`; tool/query version: `maintenance.candidates@1`. The default policy
is a validated immutable `ScoringPolicy` object. An operator-controlled server composition can inject a different,
versioned object through `build_maintenance_candidates_spec` / `run_maintenance_candidates`; it is not a model/caller
parameter and is not read from request arguments. Each profile totals 100% and is ranked independently; scores across
the two profiles are **not directly comparable**.
The tool never redistributes weights. A numeric score is emitted only when the trusted provider confirms full event and
telemetry coverage, a supported in-window GOOD/UNCERTAIN condition measurement exists, every counted fault has a
`source_event_id`, and (for `availability-v1`) the reused outage result has complete non-lower-bound session evidence.
Otherwise it returns observed factors/reasons with `INSUFFICIENT_DATA` and a null score. This avoids presenting a
partial score as comparable with a fully evidenced one.

### Factors and normalization

- **Fault recurrence (`F`)**: count qualifying FAULT-class open event rows in `[start_utc, end_utc)`. `fault_score =
  min(100, 25 × count)`. This gives 0, 25, 50, 75, then 100 at four events. Existing source-event identity prevents
  duplicate ingestion with the same source ID from being stored twice. If any counted fault lacks `source_event_id`,
  recurrence cannot be distinguished from a repeated delivery; the observed count is shown but the final score is
  withheld. Source-less historical records are not guessed to be unique.
- **Downtime (`D`)**: only for systems the event catalogue marks availability-eligible. The tool calls the existing
  `devices.offline_frequency` state machine for the same building/window; it does not reimplement session pairing.
  `downtime_score = min(100, 100 × downtime_seconds / 14,400)`, so four hours saturates this factor. A missing row,
  partial outage result, unknown initial state, orphan close, or lower-bound/open session withholds the final score;
  raw reported seconds may still be shown as evidence. Silence is not an outage.
- **Condition (`C`)**: the highest per-reading threshold score for the unit-checked metric table above. `C = 0` at/below
  warning; just above warning maps to 50; linear to 100 at critical; capped at 100. This deliberately uses no cross-unit
  comparison. `BAD` and legacy `NULL` quality are excluded. A cited `UNCERTAIN` reading can contribute but marks the row
  `LIMITED` and the result quality `UNCERTAIN`. Any legacy NULL-quality supported measurement for a candidate causes
  the condition-dependent total to be withheld.
- **Maintenance context**: unavailable in the current schema. It is not silently assigned zero and no other factor's
  weight is changed to compensate.

### Fixed profile formulas and priorities

| Profile | Eligibility | Formula |
|---|---|---|
| `availability-v1` | The catalogue declares explicit AVAILABILITY transitions for the system. | `score = 0.40 × F + 0.35 × D + 0.25 × C` |
| `fault-condition-v1` | No catalogue availability transitions. | `score = 0.60 × F + 0.40 × C` |

Priority thresholds are `HIGH >= 70`, `MEDIUM >= 40 and < 70`, otherwise `LOW`. A score is not a probability. One
isolated fault alone yields at most 15 points in the fault-condition profile and cannot produce HIGH. `rank_within_profile`
and deterministic asset-code tie ordering are returned; profile-local ranks are not a global cross-system ranking.

Manually checked examples (also pinned in `tests/fixtures/maintenance_candidates_v1.json`):

| Example | Arithmetic | Result |
|---|---|---|
| 2 faults, 2 hours downtime, condition 80, availability profile | `0.40×50 + 0.35×50 + 0.25×80` | 57.5 MEDIUM |
| 1 fault, condition 50, fault-condition profile | `0.60×25 + 0.40×50` | 35 LOW |
| 1 fault, condition 100, fault-condition profile | `0.60×25 + 0.40×100` | 55 MEDIUM |

## Contract and evidence

`build_maintenance_candidates_spec(...)` returns a `ToolSpec` named `maintenance.candidates`, version `1`, suitable for
an explicitly configured `ToolRegistry`. Required arguments are `building`, `start`, and `end`;
optional arguments are `systems` and `source`. Date strings use the building-local period semantics of
`app/domain/tools/periods.py`; timestamp inputs require an explicit offset. The maximum span is 90 days. The trusted
coverage callback, confirmed timezone list, row cap, clock, and formula are server wiring, not model parameters.

The result is the existing Tool Envelope: stable result ID, echoed normalized period, `data_as_of`, status,
limitations, source IDs, columns/rows, truncation, query version, and trace ID. Rows expose profile, score/priority (or
`INSUFFICIENT_DATA` and `score: null`), factor inputs, missing-identity counts, condition quality/sufficiency, explicit
maintenance-context absence, and reasons. Empty rows mean no qualifying evidence was found in the selected period;
they are not a proven zero-maintenance or healthy result unless coverage is confirmed.

Each row cites stored `event_record` and/or `telemetry_record` IDs. Telemetry references carry timestamp, metric code,
value, unit, and quality. Task 7's building-scoped validator now resolves typed references separately against the
correct fact table, checks building and timestamp, and verifies telemetry metric/value/unit/quality against PostgreSQL.
Different assets within the same building remain valid compound evidence. A reference to another building fails.
The result's requested building is a query scope, not an authorization claim; the application currently has no
per-user building ACL. The server caller must authorize scope before exposing this tool.

## SQL and resource bounds

SQL is static, parameterized, SELECT-only and explicitly scoped to one building and period. The window is capped at 90
days. The fault query fetches at most 20,001 rows and the condition, legacy-quality, and unsupported-unit checks each
fetch at most 2,001 per-asset rows; exceeding the 20,000-event or 2,000-asset bounds returns `CANNOT_COMPUTE`, never a
partial score. Relevant source IDs are discovered only from event/telemetry rows inside the same requested window. The
envelope builder caps returned rows (default 500). Existing time indexes are used; this package adds no migration,
table, index, endpoint, or write. The tool expects a read-only PostgreSQL cursor and the official migration-014
catalogue. It fails closed with `CATALOGUE_NOT_MIGRATED` if those columns are absent.

## Limitations and approval questions

- Do stakeholders accept the provisional weights, four-event fault saturation, four-hour downtime saturation, and
  priority thresholds? What inspection priority should each system represent?
- Should maintenance history be introduced in a separate approved schema package, and which authoritative system owns it?
- Which production connectors can attest complete event and telemetry history by source/window? How should coverage
  be captured and audited?
- Should distinct warning/critical rule configurations replace the current alert-engine thresholds, including the
  `Server Room` name special case?
- Should profile-local ranking remain, or should management approve one cross-system score model?
- Who provides trusted building authorization to the eventual runtime? The ToolSpec alone does not authorize a model's
  requested building.
- Current period evidence and stored coverage are not a live freshness guarantee. E3 freshness/liveness/gaps are not
  currently used to label every candidate row as current or stale; the tool does not emit a current healthy state, and
  no score should be read as today's asset state. A retrospective supported reading can still be stale relative to the
  current clock; freshness-aware candidate labeling is future work.
- Fact rows without `source_event_id` make repeated faults ambiguous; duplicate records cannot be collapsed by
  similarity without risking merging independent faults.

## Test coverage

`tests/unit/test_maintenance_candidates.py` covers manually pinned golden calculations, profiles, priorities, missing
factors, coverage, parameter safety, SQL bounds, profile-local ties and score-withholding rules. The real PostgreSQL
16/psycopg 3 suite `tests/integration/test_maintenance_candidates_sql.py` has eight isolated tests; each creates its own
disposable database using base seed + migrations 003–014, and the official runner only touches that test database. It
verifies actual scoped results, event/telemetry references and exact field validation, cross-building evidence rejection,
query building isolation, missing coverage/measurements, BAD exclusion, UNCERTAIN disclosure, and a real availability
candidate whose downtime comes from `devices.offline_frequency`.
It must run from an isolated Compose runner; it refuses localhost database targets.

Verified in Compose project `anzma-maintenance-candidates-20261126` (PostgreSQL 16.16, psycopg 3.2.3, `PGHOST=db`, no
published DB port):

```sh
docker compose -p anzma-maintenance-candidates-20261126 -f /tmp/anzma-maintenance-candidates-compose.yml exec -T runner \
  python -m pytest -q tests/unit/test_maintenance_candidates.py tests/unit/test_evidence_validation.py \
  tests/unit/test_tool_envelope.py tests/unit/test_doors_after_hours.py \
  tests/unit/test_devices_offline_frequency.py tests/unit/test_asset_state_grounding.py
docker compose -p anzma-maintenance-candidates-20261126 -f /tmp/anzma-maintenance-candidates-compose.yml exec -T runner \
  python -m pytest -q tests/integration/test_maintenance_candidates_sql.py \
  tests/integration/test_envelope_evidence_validation.py tests/integration/test_doors_after_hours_sql.py \
  tests/integration/test_devices_offline_frequency_sql.py
```

The recorded final results were 163 passed for the unit command and 30 passed for the PostgreSQL integration command;
the standalone asset-state grounding regression also passed 3 tests. These runs do not claim full application regression.

Further improvement should follow human approval: persist auditable source coverage if a real connector can attest it,
measure query cost on representative ≥1M-row history before adding an index, and only then consider additional metrics,
maintenance context, or runtime registration.
