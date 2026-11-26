# Task 7 hand-over

Status: checkpoint implementation and remaining verification/documentation completed in this continuation. No commit or push.

## Implementation retained from Checkpoint 1

- `app/domain/evidence/envelope_evidence.py`: read-only PostgreSQL resolution of Tool Envelope event references, result
  building scope, optional trusted server-side building allowlist, event/time checks, and cross-asset compound evidence.
- `app/domain/evidence/numeric_integrity.py`: canonical result fingerprint, metric-aware claim validation, sealing, and
  deterministic fallback.
- `app/domain/evidence/tool_answer_guard.py`: callable guard and registry seam; this is **not integrated into Chat**.
- `app/domain/evidence/evidence.py`: same-building check for legacy alert evidence. It allows cross-asset evidence within a
  building. No caller supplies a trusted building ACL today.

The four pre-existing implementation files were inspected and preserved. Active alert/incident execution remains
`app/main.py` -> `app/services/pipeline.py` -> `app/domain/evidence/evidence.py`; Chat uses the existing explanation path
through `app/api/chat_lite.py`. `rg` found no application caller of `guard_tool_answer` / `run_guarded_tool`, and
`app/domain/tools/registry.py::DEFAULT_REGISTRY` is empty. Tool Envelope Chat integration is therefore NOT implemented.

## Files changed in this continuation

- Added `tests/unit/test_evidence_validation.py` (7 focused checks).
- Added `tests/integration/test_envelope_evidence_validation.py` (5 psycopg 3 / real PostgreSQL checks).
- Updated the Day 2 disposable DB fixture to advance through the official migration runner so the current building-code
  and event-catalogue schema is present.
- Updated the Frontend/API fixture with deterministic codes for the two seed buildings, matching migration 013, while
  retaining its intended pre-014 catalogue expectations. No assertion was weakened.
- Added `docs/EVIDENCE_VALIDATION.md` and a change-coordination record.

## Verification environment and results

All database-backed commands ran from the `runner` service in disposable Compose project
`anzma-task7-validation-20261126`; `PGHOST=db`, `PGDATABASE=anzma`, PostgreSQL image 16. The isolated DB has no host port
published. Host ports 18872 and 18873 were checked free for an initial Compose build, which stalled at Dockerfile frontend
resolution and was cancelled before containers were created; the final disposable environment uses an internal-only DB
network and no published ports. The database was bootstrapped from the repository schema/seed and 003–005, then the
official migration runner applied 006–014. No shared container or database was used.

| Command / suite | Result |
|---|---:|
| `pytest -q tests/unit/test_evidence_validation.py tests/unit/test_tool_envelope.py tests/unit/test_doors_after_hours.py tests/unit/test_asset_state_grounding.py` | 67 passed |
| `pytest -q tests/unit/test_devices_offline_frequency.py` | 80 passed, 2 failed (Havana repeated-midnight expected/runtime UTC end differs by 1 hour) |
| `pytest -q tests/integration/test_envelope_evidence_validation.py` | 5 passed |
| `pytest -q tests/integration/test_envelope_evidence_validation.py tests/integration/test_doors_after_hours_sql.py tests/integration/test_devices_offline_frequency_sql.py` | 22 passed |
| `python tests/integration/test_day2_integration.py` | 43/43 passed |
| `python tests/integration/test_frontend_api.py` | 42/42 passed, including Alert evidence and Chat widget checks |
| `python tests/unit/test_day2_ahmed.py` | 29/29 passed |
| `python tests/integration/test_chat_consistency.py http://127.0.0.1:18000` | 31/31 passed against the API running inside the disposable runner container |

At the time of the Task 7 verification, two `devices.offline_frequency` assertions expected a Havana fallback at
`2026-11-02T05:00:00Z`. The follow-up corrected those T2 expectations to the user-verified tzdb 2026f rule and added an
independent synthetic 25-hour fallback test; production period code and the S-T1 fixture were left unchanged. An earlier
Day 2 run against an incomplete 011-only fixture produced 31/43; after the test fixture used the official migration runner
for current schema, the same suite passed 43/43. The earlier Frontend/API run exposed an outdated null-building fixture;
the fixture now assigns the migration-013 seed codes and the suite passes without changing its assertions.

See `docs/EVIDENCE_VALIDATION.md` for interfaces, security boundaries, test commands, and limitations. `DEFAULT_REGISTRY`
remains empty and no active request path invokes the new Tool Envelope guard. This work is uncommitted and unpushed.

## Continuation: maintenance.candidates

Task 8 adds `app/domain/tools/maintenance_candidates.py` as a deterministic, versioned proposal. It uses the existing
ToolSpec/Envelope and outage-session logic; it adds no migration, endpoint, persistence, or `DEFAULT_REGISTRY` wiring.
Evidence references now include verified `telemetry_record` fields as well as event records; the validator checks
timestamp, metric, value, unit, quality, and same-building scope. Cross-asset evidence within one building remains valid.

Policy `maintenance-priority-proposal-1` remains **pending human approval**. Scores require trusted server-side coverage
for both events and telemetry across the whole selected interval; no such production provider is configured, so the
default execution withholds scores. See `docs/MAINTENANCE_CANDIDATES.md` for the exact factors, fixed weights, limits,
evidence semantics, tests, and open decisions. The PostgreSQL integration fixture creates a unique database on a
disposable PostgreSQL 16 container and uses psycopg 3; it refuses localhost targets.

Runtime integration remains pending: `DEFAULT_REGISTRY` is empty and active Chat does not invoke the Tool Envelope
guard. This work remains uncommitted and unpushed.

### Task 8 verification (2026-11-26)

The maintenance candidate integration suite passed 8 tests against disposable PostgreSQL 16.16 using psycopg 3.2.3.
Combined with Tool Envelope evidence, Task 5, Task 6, and grounding unit/integration regressions, the final commands
reported 163 unit tests passed and 30 PostgreSQL integration tests passed. The targeted `test_asset_state_grounding.py`
run separately also passed 3 tests. All runs used Compose project `anzma-maintenance-candidates-20261126`; DB service
was `postgres:16`, runner `PGHOST=db`, and no host DB port was published. This verifies compatibility with these named
regressions, not full application regression or Chat runtime wiring. Reproducible commands are in
`docs/MAINTENANCE_CANDIDATES.md`'s test-coverage section; no production registry/Chat integration was attempted.
The existing Ahmed alert/grounding harness was run as `python tests/unit/test_day2_ahmed.py` (29/29). A preliminary
`pytest` invocation against that script-format harness yielded 1 setup error because pytest collected its decorator
factory `test(name)`; rerunning it with its documented Python entry point passed, with no code change required.
