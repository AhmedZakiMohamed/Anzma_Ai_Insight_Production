# devices.offline_frequency (design note and fixture review)

- **Code:** `app/domain/tools/devices_offline_frequency.py` (shared: `periods.py` `build_period`, `coverage.py`, `envelope.py`).
- **Tests:** `tests/unit/test_devices_offline_frequency.py` (no DB; includes golden-fixture replay) and
  `tests/integration/test_devices_offline_frequency_sql.py` (disposable PostgreSQL, applies migrations 003-014).
- **Depends on:** migration 014 (`event_class`, `outage_class`, `resolves_event_type_id`). Without it the tool returns
  `CANNOT_COMPUTE` / `CATALOGUE_NOT_MIGRATED` instead of guessing event names.
- **Not wired:** `DEFAULT_REGISTRY` is still empty, like `doors.after_hours`. See "Wiring".

## What it computes

One row per device (asset + source) with explicit offline/online evidence in the period, ordered by
`outages_started_in_period` DESC (or `downtime_seconds` with `rank_by`), then building, source, external id, asset code.

| Requirement | Implementation |
|---|---|
| Session grouping | Per device state machine (D-06): a repeated `FAULT_OPEN` (AVAILABILITY) while OFFLINE joins the open session (`repeated_start_events`), it never opens a new one. Cycles are never merged because they are close (D-07); `flapping` is `not_evaluated`. |
| Open-ended outages | A session with no recovery stays open; no recovery time is invented. Duration is measured to the earliest of period end, the source `data_as_of` (from the trusted coverage provider) and the query time, and flagged `duration_is_lower_bound` / `OPEN_OUTAGE_LOWER_BOUND` (D-09). The session reports `cutoff_utc` and `cutoff_reason`. |
| Per-device metrics | `outages_started_in_period`, `outages_ongoing_at_period_start`, `downtime_seconds_in_period`, `open_at_period_end`, `state_at_period_end`, and `sessions[]` each with `start_utc`, `end_utc`, `overlap_seconds`, `duration_seconds`, `evidence_refs`. |
| Silence is not an outage | Only catalogue AVAILABILITY transitions are read (`camera_offline/online`, `link_down/up`). No events = no row, never a zero or an outage. HVAC/fire fault pairs are never evidence (D-05). A silent source stays a coverage gap (`PARTIAL`). |
| Started before the period | Unbounded look-back (open question d), bounded in volume to the last recovery, the first later start and the last earlier event per device. Only the overlap is counted; the session is `ongoing_at_period_start`, not "started". No earlier transition = `initial_state: UNKNOWN` + `INITIAL_STATE_UNKNOWN`. |
| Data quality | Duplicate deliveries `(source_id, source_event_id)` collapse (`duplicate_events_ignored`); events without identity are kept individually, `MISSING_EVENT_IDENTITY`; orphan recoveries are listed in `orphan_offline_end_refs`, `ORPHAN_OFFLINE_END`. Period is half-open `[start, end)` in building time (D-10). |
| Evidence | Every row and session carries `evidence_refs` = `event_record` ids (`kind`, `id`, `observed_at`, `event_type`) back to `event_records.event_id`; `initial_state_evidence_refs` explains the starting state. |
| Completeness | `COMPLETE` only with a trusted `CoverageProvider` confirming the whole period, an explicit building timezone, and all requested systems eligible; otherwise `PARTIAL` with limitations. Non-eligible systems only: `UNSUPPORTED`. Bad timezone/dates/unknown building: `CANNOT_COMPUTE`. `completeness` is carried by `status`, `complete`, `limitations`. |

Parameters: `building`, `start`, `end` (required); `systems`, `source`, `rank_by` (optional). `coverage_provider`,
`confirmed_timezone_buildings` and `max_rows` are trusted wiring, not tool arguments.

## Golden fixtures (fixture 0.3; see `docs/ADR_GOLDEN_FIXTURES_RESOLUTION.md`)

1. **COMPLETE needs a coverage provider.** The replay injects a fixture-backed provider; without one the tool is `PARTIAL`.
2. **S-H1 (resolved).** Coverage ends before the period end, so the expectation is now `PARTIAL`, `complete` false, `state_at_period_end` UNKNOWN. New row field `state_at_period_end`; `open_at_period_end` only means "no recovery observed up to the cutoff".
3. **S-I (open, cosmetic).** The fixture lists EV-I-001 as evidence; the look-back keeps only the recovery that proves ONLINE (EV-I-002). Both sides asserted; **NEEDS_DECISION** (initial-state evidence granularity).
4. **S-M (open).** The fixture ranks across two buildings; the tool takes one building and ranks per building (tested per building, same order). **NEEDS_DECISION** (cross-building ranking is a product scope question).
5. **S-N (open, schema).** Contract 1.0 has no timestamp quality or quarantine path; the tool returns `COMPLETE` for the valid events where the fixture expects `PARTIAL`. `BLOCKED_BY_SCHEMA`.
6. **S-J.** `uncovered_seconds_in_period` has no envelope field; the gap shows as `COMPLETION_NOT_CONFIRMED`.
7. **S-P1/P2/Q1/Q2/R/T1/T2** have no entries in `expectations.json`; asserted from ADR rules. T2 (Havana, tzdb 2026f, fall-back cancelled, 24 h) is skipped on hosts whose tz database predates 2026f; `SyntheticTimezoneTests` cover the gap and overlap rules independently of the host.
8. Evidence ids are `event_records.event_id`, not fixture ids (naming only).

## Known limitations

- Look-back and period events are fetched into Python (needed for evidence); apply `statement_timeout` in the cursor factory.
- `flapping` is always `not_evaluated` until an approved window exists (D-07).
- The earlier-state anchor is per `(asset, source)`; a device reported by two sources is two rows.

## Running the PostgreSQL test

Disposable database only. `PGPASSWORD=... python -m unittest tests.integration.test_devices_offline_frequency_sql -v`
(needs `psycopg`). It was executed on PostgreSQL 16 in this environment with a local psql-backed stand-in for `psycopg`
because the driver could not be installed; re-run it once with the real driver.

## Wiring

```python
spec = build_devices_offline_frequency_spec(cursor_factory,   # context manager -> dict-row cursor, READ ONLY transaction
                                            coverage_provider=None, confirmed_timezone_buildings=frozenset(), max_rows=None)
DEFAULT_REGISTRY.register(spec)
```
