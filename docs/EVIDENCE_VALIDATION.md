# Task 7 — Evidence and numeric validation

Status: checkpoint implementation and focused tests are present. This note describes the current code, including what is
not wired into a production request path. It is not a claim that Tool Envelope Chat integration is complete.

## Current execution paths

The existing alert path is active: `app/main.py` alert/incident routes call `app/services/pipeline.py`; `pipeline.explain_alert`
calls `app/domain/evidence/evidence.py::alert_evidence`, while incident evidence calls `incident_evidence`. Those functions
re-read referenced event/telemetry rows from PostgreSQL and check fact identity, timestamp/value/event type, and building.
The alert asset and cited facts must resolve to the same building. Asset identity need not match: compound evidence from
several assets in one building is valid. The route currently supplies no `authorized_building_codes` ACL context.

`app/domain/evidence/tool_answer_guard.py::guard_tool_answer` and `run_guarded_tool` are callable seams only. They are not
called from `app/api/chat_lite.py`, `app/main.py`, or another active app path. `app/domain/tools/registry.py::DEFAULT_REGISTRY`
is an empty `ToolRegistry`; the tool modules are not registered there. Therefore no production Tool Envelope request is
currently protected by this guard, and no public response uses its `GuardOutcome` shape.

## Tool Envelope evidence verification

`validate_envelope_evidence(cur, result, ...)` accepts an `Envelope` or its dictionary form. It requires one unambiguous
`parameters.building` (or `building_code`) scope. If supplied, `authorized_building_codes` is trusted server context only;
never take it from a model answer or request arguments. It requires a dict-row cursor in a PostgreSQL read-only transaction
by default, supports `event_record` and `telemetry_record` references, and accepts canonical positive decimal BIGINT IDs
represented as strings. A row must carry evidence, its `building_code` must equal the result scope, nested reference lists
must be subsets of the row's references, and every unique typed reference must exist in its corresponding fact table under
an asset/location in the same building. Supplied observation time and event type are checked against PostgreSQL. For
telemetry, supplied metric code, value, unit, and quality are also checked. IDs are keyed by `(kind, id)` so an event and
telemetry row with the same numeric ID cannot collide. Each unique typed ID is fetched once in parameterized batches.
Asset equality is intentionally not enforced. Invalid data or database lookup errors fail closed; database errors are
represented by issue codes rather than leaked exception details.

Blocking issue codes are defined in `EvidenceCode` in `app/domain/evidence/envelope_evidence.py` and include missing or
unresolved building scope, unauthorized building, malformed/unsupported references, missing evidence, missing database
rows, cross-building evidence, timestamp/type/metric/value/unit/quality mismatch, non-read-only transaction and lookup
failure. Duplicate refs are non-blocking. `evidence.py::_verify` applies the same-building boundary to the established
alert evidence JSON format; it continues to allow cross-asset evidence within that boundary.

## Numeric claims, integrity fingerprint, and fallback

`app/domain/evidence/numeric_integrity.py` is pure Python. `result_fingerprint` returns `sha256:<hex>` over policy 1 and
the authoritative fields listed in `FINGERPRINT_FIELDS`; it excludes only `generated_at` and `trace_id`. Object keys and
reference sets are canonicalized; meaningful row/session/column order remains meaningful. Non-finite numbers are rejected.
`seal_result` captures a JSON copy and fingerprint before an answer generator can mutate the model-facing copy.

`validate_numeric_claims` compares extracted claims to registered metric fields for `doors.after_hours` and
`devices.offline_frequency`, plus shared row/count/evidence/ranking/identity values. It does not treat arbitrary digits in
evidence text as authority. It reports unsupported or conflicting claims with `NumericIssueCode`; omitted measurement units
and unconfirmed zero in incomplete data are non-blocking warnings.

`deterministic_fallback` summarizes original envelope values. The guard accepts a model answer only when the authoritative
fingerprint, presented result (when supplied), evidence check and numeric checks pass. Otherwise it chooses a deterministic
fallback. If evidence or the authoritative result is untrusted, it produces a no-values `unverifiable_text` message instead.
When no model answer exists, the source is `rules`; a rejected model answer uses `fallback`.

## Tests and reproducible commands

The focused tests are `tests/unit/test_evidence_validation.py` and
`tests/integration/test_envelope_evidence_validation.py`. The integration fixture creates and drops its own database, uses
psycopg 3, and refuses localhost targets; it exercises actual PostgreSQL facts, trusted building authorization,
cross-building rejection, and valid compound evidence across two assets in one building.

Verified in disposable Compose project `anzma-task7-validation-20261126` (runner service with `PGHOST=db`; database service
is PostgreSQL 16; no host DB port is published):

```sh
python -m pytest -q tests/unit/test_evidence_validation.py tests/unit/test_tool_envelope.py tests/unit/test_doors_after_hours.py tests/unit/test_asset_state_grounding.py
python -m pytest -q tests/integration/test_envelope_evidence_validation.py
python -m pytest -q tests/integration/test_envelope_evidence_validation.py tests/integration/test_doors_after_hours_sql.py tests/integration/test_devices_offline_frequency_sql.py
python tests/integration/test_day2_integration.py
python tests/integration/test_frontend_api.py
python tests/unit/test_day2_ahmed.py
python tests/integration/test_chat_consistency.py http://127.0.0.1:18000
```

Results from the final applicable runs: unit selection 67 passed; Task 7 PostgreSQL tests 5 passed; combined PostgreSQL
Tool Envelope SQL tests 22 passed; Day 2 integration 43/43; Frontend/API 42/42; Ahmed grounding unit harness 29/29; live
Chat/dashboard consistency 31/31. At that run, the existing `tests/unit/test_devices_offline_frequency.py` had 80 passed,
2 failed because its Havana T2 expectation predated tzdb 2026f. The follow-up correction updates that fixture expectation
and adds a synthetic timezone test; see `docs/DEVICES_OFFLINE_FREQUENCY.md` and the current Task 7 handover.

## Limitations and follow-up

- Tool Envelope guard/registry/Chat wiring remains pending; do not describe it as integrated until the registry and active
  chat execution path call the guarded seam.
- Building scope is evidence consistency, not per-user authorization. The current application passes no trusted building
  allowlist to legacy alert evidence checks; user/building ACL design is still required before claiming that boundary.
- `GuardOutcome.to_response` adds result/fingerprint/integrity data. Any adoption that exposes it publicly requires the
  relevant API response-shape review and approval; this continuation does not change a public response.
- Numeric extraction is intentionally conservative and heuristic. Entity attribution is per sentence/line; compound
  durations, averages, differences, and most negation are not interpreted. The deterministic fallback is English-only.
- Metric specifications are code-registered for the current tools. New tools/metrics need explicit specifications and
  deterministic positive/negative tests before model answers can use them.
- PostgreSQL integration here verifies evidence resolution and building scope; it does not prove a complete Tool Envelope
  chat flow because that flow is not wired.
