# Day 3 Operational Data Validation

**Report date:** 22 November 2026
**Validated application code commit:** `60579a0f4029c4f3b49376abc338d9f34baac0c3` on `main`
**Purpose:** Record the known checks and open validation limits for the current local working release. This is not a claim that live vendor/sensor data is connected.

## Data source and interpretation

The currently visible dashboard data is seed/legacy operational data. The environment has no registered ingestion source and no connected external building sensor/vendor feed. Dashboard incidents and alerts are therefore not evidence of a live external integration. The earlier S1 end-to-end checks used an isolated test database named `anzma_e2e`; that database was dropped after the checks.

The freshness dashboard reported no persisted series snapshot and no registered ingestion sources in the observed state. The panel explains that seed/legacy series may not have source or building codes. “No data,” stale, and silent-source indicators describe data availability/evidence and do not, by themselves, confirm equipment failure.

## Operational data checks

The following snapshot counts and measurements come from the earlier read-only review of the local operational database/dashboard, before the current Task 4 source changes. They are useful as a recorded baseline, but should be re-queried against the final release candidate before sign-off.

| Check | Observed result | Assessment |
|---|---:|---|
| Stored events | 1,666 | Present in reviewed snapshot. |
| Telemetry rows | 8,112 | Present in reviewed snapshot. |
| Alerts | 10 | Present in reviewed snapshot. |
| Incidents | 8 | Present in reviewed snapshot. |
| Alert evidence references | 29/29 resolved | 12 event references and 17 telemetry references pointed to stored source records in reviewed snapshot. |
| Duplicate alert groups | 0 | No duplicate groups observed in that snapshot; unique alert index exists. |
| Dashboard assets | 12 | 6 normal, 2 warning, 2 critical, and 2 no-data in that snapshot; no unknown asset status was counted. |
| Five system rollups | HVAC Warning; Network Critical; Access Control Warning; CCTV Warning; Fire Alarm Critical | Values reflect seeded/legacy dashboard data, not connected sensor state. |

“Events,” “alerts,” and “incidents” remain separate records/concepts. Alert evidence references are traceability links to source records; they do not independently prove the physical root cause.

## State and freshness presentation

- Critical and warning conditions are represented separately from recovered/history context.
- Missing, stale, and unknown/unavailable readings must not be interpreted as normal readings.
- The observed freshness panel showed 0 fresh, 38 missing, 9 stale, and 0 unknown series in the reviewed dashboard state. It showed no persisted series snapshot and no registered source liveness records.
- A source-silent or suspected-gap label is evidence of missing expected data, not confirmation that equipment is offline.
- The current review did not establish any live source-to-canonical asset mapping or validate measurements received from a real vendor.

## Engineering units and bandwidth discrepancy

The reviewed `bandwidth_util` telemetry uses `%`. In that snapshot, 1,252 rows ranged from 15.79% to 72.36%, with a mean of 41.67%; none was outside 0–100%. No bandwidth alert fired because the observed maximum was below the configured warning threshold.

Recorded thresholds were warning at 80% after three sustained readings, critical at 90%, and an anomaly trigger at a deviation of 15 percentage points. These threshold semantics and the “sustained” evaluation should be confirmed with engineering against the current code and data. Ahmed reported verbal approval from Engineer Mohsen on 18 November; a written approval artifact was not located. Thresholds were not changed as part of this work. Any change requires documented engineering approval, expected-impact analysis, and regression coverage.

## Duplicate detection and evidence

The earlier database review found a unique alert constraint/index and no duplicate alert groups in its snapshot. The isolated S1 scenario verified its configured 14 evidence references against stored records and passed its duplicate-detection checks. These checks do not prove that all production detection schedules, all historical records, or every concurrent ingestion race are duplicate-free.

## AI explanation and recommendation validation

- Deterministic fallback completed the S1 scenario and retained supporting evidence links (14/14 resolved).
- The operator reports that Qwen answered successfully in browser chat. The chat response's model/source metadata was not supplied for independent verification.
- Separate live-Qwen S1 alert-explanation attempts returned `ai_source=fallback` and `model_used=None`. This scripted alert path was not demonstrated successfully and should not be conflated with the reported browser-chat success.
- Current safety behavior marks corrective actions provisional unless a real approver and approval reference are present; draft actions are excluded from recommendations.
- The grounding indicator reports the configured evidence checks. It is not proof of semantic correctness.
- The unsupported-causality validator is a narrow phrase guard, not a comprehensive fact checker. AI output still requires evidence inspection and engineering review.

## Test evidence and currency

| Evidence | Result | Currency / boundary |
|---|---:|---|
| Unit suite `test_day2_ahmed.py` | 29/29 passed | Latest recorded run after Task 4 safety changes. |
| Frontend API suite `test_frontend_api.py` | 42/42 passed | Latest recorded run after Task 4 changes with test auth environment set. |
| S1 fallback scenario | 14/14 passed | Isolated test DB, later dropped. |
| Browser chat using Qwen | Operator-reported successful | Model/source metadata and recording were not supplied for independent verification. |
| S1 live-model alert explanation | Fell back | This distinct path is not a live-Qwen pass, even though scenario assertions accepted fallback. |
| Other suites | 43/43 and 23/23 passed | Historical runs before latest Task 4 edits; rerun before release. |

## Open work before sign-off

1. Re-query event/telemetry/alert/incident counts and evidence references on the final immutable release-candidate database snapshot.
2. Recheck dashboard state and time filters through the browser recording.
3. Confirm the bandwidth thresholds and obtain a written approval artifact; do not silently modify behavior.
4. Capture and inspect the browser chat's model/source label and answer evidence; separately complete the live-Qwen S1 alert explanation and inspect its model ID, input measurements, grounding evidence, and recommendations.
5. Have an independent reviewer compare displayed units and states with stored records.
6. Keep external connector and live-sensor claims out of the release notes until a real source is configured and verified.
